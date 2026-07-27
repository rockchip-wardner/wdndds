from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Self


class Subscriber:
    """High-level wrapper around a FastDDS Subscriber."""

    def __init__(self, raw: Any, *, owned: bool = False) -> None:
        self._raw = raw
        self._owned = owned

    @classmethod
    def _create(cls, participant: Any, qos: Any | None) -> Self:
        from ._fastdds import get

        fd = get()
        if qos is None:
            qos = fd.SubscriberQos()
        raw = participant.create_subscriber(qos, None, fd.STATUS_MASK_NONE)
        if raw is None:
            raise RuntimeError("failed to create subscriber")
        return cls(raw, owned=True)

    @property
    def raw(self) -> Any:
        return self._raw

    def create_reader(
        self,
        topic: Any,
        qos: Any | None = None,
    ) -> Reader:
        from ._fastdds import get

        fd = get()
        if qos is None:
            qos = fd.DataReaderQos()
        raw = self._raw.create_datareader(
            topic.raw if hasattr(topic, "raw") else topic,
            qos,
            None,
            fd.STATUS_MASK_NONE,
        )
        if raw is None:
            raise RuntimeError("failed to create data reader")
        return Reader(raw, owner=self)

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        with suppress(Exception):
            self._raw.get_participant().delete_subscriber(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Reader:
    """High-level wrapper around a FastDDS DataReader."""

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

    def take(self, max_samples: int = 100) -> list[tuple[Any, Any]]:
        return self._raw.take(max_samples)

    def read(self, max_samples: int = 100) -> list[tuple[Any, Any]]:
        return self._raw.read(max_samples)

    def __iter__(self) -> Iterator[tuple[Any, Any]]:
        return iter(self.take())

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        if self._owner is not None:
            with suppress(Exception):
                self._owner.raw.delete_datareader(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
