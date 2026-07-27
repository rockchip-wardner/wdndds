from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Self


class Topic:
    """High-level wrapper around a FastDDS Topic."""

    def __init__(self, raw: Any, *, owned: bool = False) -> None:
        self._raw = raw
        self._owned = owned

    @classmethod
    def _create(
        cls,
        participant: Any,
        name: str,
        type_name: str,
        qos: Any | None,
    ) -> Self:
        from ._fastdds import get

        fd = get()
        if qos is None:
            qos = fd.TopicQos()
        raw = participant.create_topic(name, type_name, qos, None, fd.STATUS_MASK_NONE)
        if raw is None:
            raise RuntimeError(f"failed to create topic '{name}'")
        return cls(raw, owned=True)

    @property
    def raw(self) -> Any:
        return self._raw

    @property
    def name(self) -> str:
        return self._raw.get_name()

    @property
    def type_name(self) -> str:
        return self._raw.get_type_name()

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        with suppress(Exception):
            self._raw.get_participant().delete_topic(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
