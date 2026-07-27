from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from ._fastdds import get

if TYPE_CHECKING:
    from typing import Self

    from .publisher import Publisher
    from .subscriber import Subscriber
    from .topic import Topic


class Participant:
    """High-level wrapper around a FastDDS DomainParticipant.

    Use as a context manager to ensure cleanup, or call :meth:`close` manually.
    """

    def __init__(
        self,
        domain_id: int = 0,
        qos: Any | None = None,
        *,
        name: str | None = None,
    ) -> None:
        fd = get()
        if qos is None:
            qos = fd.DomainParticipantQos()
        raw = fd.DomainParticipantFactory.get_instance().create_participant(
            domain_id, qos, None, fd.STATUS_MASK_NONE
        )
        if raw is None:
            raise RuntimeError(f"failed to create DomainParticipant on domain {domain_id}")
        self._raw = raw
        self._domain_id = domain_id
        self._owned = True
        if name:
            self._raw.set_name(name)

    @property
    def raw(self) -> Any:
        return self._raw

    @property
    def domain_id(self) -> int:
        return self._domain_id

    def create_topic(
        self,
        name: str,
        type_name: str,
        qos: Any | None = None,
    ) -> Topic:
        from .topic import Topic

        return Topic._create(self._raw, name, type_name, qos)

    def create_publisher(self, qos: Any | None = None) -> Publisher:
        from .publisher import Publisher

        return Publisher._create(self._raw, qos)

    def create_subscriber(self, qos: Any | None = None) -> Subscriber:
        from .subscriber import Subscriber

        return Subscriber._create(self._raw, qos)

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        with suppress(Exception):
            get().DomainParticipantFactory.get_instance().delete_participant(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
