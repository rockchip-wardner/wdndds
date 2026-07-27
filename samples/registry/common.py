"""Shared constants and payload helpers for the registry sample."""

from __future__ import annotations

import json
from datetime import datetime, timezone

TOPIC_NAME = "wdiot/registry"
TYPE_ALIAS = "registry"


def make_registration(
    node_id: str,
    node_name: str,
    location: dict,
    capabilities: list,
    ttl: int = 30,
) -> dict:
    return {
        "node_id": node_id,
        "node_name": node_name,
        "location": dict(location),
        "capabilities": list(capabilities),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ttl": ttl,
    }


def encode(reg: dict) -> str:
    return json.dumps(reg, ensure_ascii=False, sort_keys=True)


def decode(payload: str) -> dict:
    return json.loads(payload)
