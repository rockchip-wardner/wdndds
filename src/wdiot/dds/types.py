from __future__ import annotations

from typing import Any

from ._fastdds import get


def register_string_type(participant: Any, alias: str) -> str:
    """Register eProsima's built-in StringTopicDataType under a unique name.

    Returns the registered type name so callers can pass it to
    :meth:`wdiot.dds.Participant.create_topic`.

    Example
    -------
    >>> tname = participant.register_string_type("registry")
    >>> topic = participant.create_topic("wdiot/registry", tname)
    >>> writer.write(json.dumps({"hello": "world"}))
    """
    fd = get()
    type_name = f"wdiot::string::{alias}"
    ts = fd.StringTopicDataType()
    ts.set_name(type_name)
    participant.register_type(ts)
    return type_name


def get_type_support(topic: Any) -> Any:
    """Return the TypeSupport attached to a Topic (for introspection)."""
    return topic.get_type_support()
