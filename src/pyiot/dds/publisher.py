from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from ._fastdds import get
from .exceptions import check

if TYPE_CHECKING:
    from typing import Self


class Publisher:
    """High-level wrapper around a FastDDS Publisher."""

    def __init__(self, raw: Any, *, owned: bool = False) -> None:
        self._raw = raw
        self._owned = owned

    @classmethod
    def _create(cls, participant: Any, qos: Any | None) -> Self:
        fd = get()
        if qos is None:
            qos = fd.PublisherQos()
        raw = participant.create_publisher(qos, None, fd.STATUS_MASK_NONE)
        if raw is None:
            raise RuntimeError("failed to create publisher")
        return cls(raw, owned=True)

    @property
    def raw(self) -> Any:
        return self._raw

    def create_writer(
        self,
        topic: Any,
        qos: Any | None = None,
    ) -> Writer:
        fd = get()
        if qos is None:
            qos = fd.DataWriterQos()
        raw = self._raw.create_datawriter(
            topic.raw if hasattr(topic, "raw") else topic,
            qos,
            None,
            fd.STATUS_MASK_NONE,
        )
        if raw is None:
            raise RuntimeError("failed to create data writer")
        return Writer(raw, owner=self)

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        with suppress(Exception):
            self._raw.get_participant().delete_publisher(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Writer:
    """High-level wrapper around a FastDDS DataWriter."""

    def __init__(
        self,
        raw: Any,
        *,
        owner: Any = None,
        owned: bool = True,
    ) -> None:
        self._raw = raw
        self._owner = owner
        self._owned = owned

    @property
    def raw(self) -> Any:
        return self._raw

    def write(self, data: Any) -> None:
        rc = self._raw.write(data)
        check(rc, "DataWriter.write", get().RETCODE_OK)

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        if self._owner is not None:
            with suppress(Exception):
                self._owner.raw.delete_datawriter(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
