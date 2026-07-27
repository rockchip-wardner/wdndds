from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .capabilities import Capability


@dataclass(frozen=True)
class Event:
    """A discovery event delivered to scan callbacks.

    Attributes
    ----------
    kind:
        Either ``"added"`` (device/capability appeared) or ``"removed"``
        (device went away). Removal is best-effort: it relies on FastDDS
        built-in participant liveliness notifications.
    capability:
        The specific capability that triggered the event. Currently always
        ``None`` because discovery is device-granular. Reserved for future
        capability-level events.
    """

    kind: Literal["added", "removed"]
    capability: Capability | None = None
