"""FastDDS Python topic-type registration helpers.

Module-level helpers the rest of :mod:`wdndds.dds` uses to bind a DDS
``TopicDataType`` alias for the JSON-string payloads we publish on
``wdndds/registry`` / ``wdndds/capability/<type>`` / ``wdndds/reply/<uuid>``.

The upstream ``eprosima::fastdds::dds::StringTopicDataType`` is exposed by
the C++ FastDDS, but the official Python bindings (the SWIG module we
bundle) **do not** ship a string-typed TopicDataType subclass — SWIG
only generates wrappers for a handful of stock types. And the broken
``fastdds.TypeSupport(TopicDataType *)`` SWIG overload won't accept
custom subclasses defined in another SWIG module either.

We work around both limitations by shipping a small C++ helper
(``.venv/src/wdndds_cxx/``) that defines ``WdnddsStringTopicDataType``
and exposes ``wdndds_register_string_type(participant, alias)`` —
taking care of the TypeSupport wrap and registration on the C++ side.
The Python caller just sees a normal string return value.
"""

from __future__ import annotations

from typing import Any


def _import_wdndds_string() -> Any:
    """Return the bundled ``wdndds_string`` SWIG module, or ``None``.

    The module is preloaded by :func:`wdndds.dds._fastdds.get` via
    ``importlib.import_module`` after we add ``_native/`` to
    ``sys.path``. If the wheel is missing the bundled custom type
    (very old build, or an unrelated system fastdds), this returns
    ``None`` and the caller raises a clear error.
    """
    import sys

    if "wdndds_string" in sys.modules:
        return sys.modules["wdndds_string"]
    try:
        import wdndds_string  # noqa: F401  — populated by _fastdds.get
        return wdndds_string
    except ImportError:
        return None


def register_string_type(participant: Any, alias: str) -> str:
    """Register the bundled string type under ``alias`` and return it.

    The returned string is the registered type name
    (``"wdndds::string::<alias>"``); pass it to
    ``create_topic(name, type_name, ...)``.
    """
    wds = _import_wdndds_string()
    if wds is None:
        raise RuntimeError(
            "wdndds.dds.types.register_string_type: bundled "
            "WdnddsStringTopicDataType not available. This wdndds "
            "wheel appears to be missing the C++ helper (reinstall a "
            "wheel that matches your platform: "
            "aarch64 Debian 12 / glibc 2.36 / Python 3.11)."
        )
    # The C++ helper takes an opaque ``void *`` and does the static_cast
    # to ``DomainParticipant *`` itself, since SWIG can't see the type.
    raw = participant.__class__.__name__  # just to assert participant is real
    try:
        # Some SWIG proxy classes expose ``this`` attribute as a SWIG pointer.
        opaque = getattr(participant, "this", participant)
        return wds.wdndds_register_string_type(opaque, alias)
    except TypeError:
        # Fallback: try the proxy directly (some SWIG versions allow this).
        return wds.wdndds_register_string_type(participant, alias)


def create_string_data() -> Any:
    """Allocate an empty string sample for publishing."""
    wds = _import_wdndds_string()
    if wds is None:
        raise RuntimeError("wdndds_string helper not available")
    return wds.wdndds_create_string_data()


def delete_string_data(data: Any) -> None:
    """Free a string sample after FastDDS is done with it."""
    wds = _import_wdndds_string()
    if wds is None:
        return
    wds.wdndds_delete_string_data(data)


def get_type_support(topic: Any) -> Any:
    """Return the TypeSupport attached to a Topic (for introspection)."""
    return topic.get_type_support()