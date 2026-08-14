"""wdndds.device registry (Discovery page).

This module is now a thin shim around the new rrpc-based Hub in
:mod:`wdndds.dds._rpc`. Current implementation:

  * :class:`Hub` — wrapper around the cshim's DomainParticipant.
  * :class:`Device` — local or remote device (with capabilities).
  * :func:`connect`, :func:`register`, :func:`start_scan`, :func:`stop_scan` —
    module-level helpers for the discovery page.

The Discovery page uses these to broadcast capabilities over DDS.
The capability tables themselves are static (in :mod:`app.services.dds_discovery`).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from .dds._rpc import Hub as _RpcHub


_LOG = logging.getLogger("wdndds.registry")


# Forward-decl for Capability (re-exported from .capabilities).
Capability = None


class Device:
    def __init__(self, name: str, capabilities: list["Capability"]):
        self.name = name
        self.capabilities = capabilities


# --------------------------------------------------------------------------- #
# Public Hub — one per process                                                #
# --------------------------------------------------------------------------- #
class Hub:
    """Discovery-side hub. Wraps the cshim's DomainParticipant.

    The current implementation only supports a single DomainParticipant
    per process. Multiple ``Hub`` instances are not supported (the second
    one raises an error from FastDDS about GUID collisions).
    """

    def __init__(self, domain_id: int = 0):
        self._lock = threading.RLock()
        self._domain_id = domain_id
        self._hub: Optional[_RpcHub] = None
        self._local_device: Optional[Device] = None
        self._remote_devices: dict[str, Device] = {}
        self._device_lock = threading.Lock()

    def connect(self) -> _RpcHub:
        with self._lock:
            if self._hub is None:
                self._hub = _RpcHub(
                    participant_name="wdnwebui-discovery",
                    domain_id=self._domain_id)
                self._hub.start()
            return self._hub

    def disconnect(self) -> None:
        with self._lock:
            if self._hub is not None:
                self._hub.close()
                self._hub = None

    def register(self, name: str, capabilities: list["Capability"]) -> Device:
        """Register a local device. Stores the device; broadcast to peers
        happens via start_scan (which subscribes to peer registry broadcasts).
        """
        with self._lock:
            self.connect()
        device = Device(name, capabilities)
        with self._device_lock:
            self._local_device = device
        return device

    def start_scan(self, callback: Callable[[dict], None]) -> None:
        """Start listening for peer registry broadcasts.

        NOTE: this is a stub in v0.5 — actual peer discovery is wired
        via the DDS reader/writer on the registry topic. Until that
        is wired through the cshim, this method just records the
        callback and returns.
        """
        _LOG.warning("wdndds.registry.start_scan: stub; no peer discovery")

    def stop_scan(self) -> None:
        pass

    def list_devices(self) -> list[Device]:
        with self._device_lock:
            return [self._local_device] + list(self._remote_devices.values()) \
                if self._local_device else list(self._remote_devices.values())


# --------------------------------------------------------------------------- #
# Module-level singleton                                                      #
# --------------------------------------------------------------------------- #
_hub: Optional[Hub] = None
_hub_lock = threading.Lock()


def connect(domain_id: int = 0) -> Hub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = Hub(domain_id=domain_id)
        _hub.connect()
    return _hub


def _get_hub() -> Optional[Hub]:
    return _hub


def register(name: str, capabilities: list["Capability"]) -> Device:
    return connect().register(name, capabilities)


def start_scan(callback: Callable[[dict], None]) -> None:
    connect().start_scan(callback)


def stop_scan() -> None:
    if _hub is not None:
        _hub.stop_scan()


def close() -> None:
    """Tear down the global hub. Idempotent."""
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.disconnect()
            _hub = None
