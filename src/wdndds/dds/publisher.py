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
        # See ``topic.py`` — modern FastDDS Python bindings only support
        # the 3-arg ``create_publisher(qos) -> Publisher*`` form; the
        # listener + StatusMask overloads require a ReturnCode_t& that
        # SWIG can't satisfy from Python.
        raw = participant.create_publisher(qos)
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
        # See ``topic.py`` — modern FastDDS Python bindings only support
        # the 3-arg ``create_datawriter(topic, qos) -> DataWriter*``
        # form from Python.
        raw = self._raw.create_datawriter(
            topic.raw if hasattr(topic, "raw") else topic,
            qos,
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
        # Fast-DDS-Python 1.x ``DataWriter::write(void *)`` 要求一个
        # SwigPyObject（指向 std::string），不接受 Python 原生 str；
        # 2.x 之后 ``write`` 自带 str → CDR 转换，但 1.x 没有 —— 必须
        # 走 C++ helper ``wdndds_create_string_data_with(value)`` 在 C
        # 端建一个 std::string，把 SwigPyObject 喂给 write()，再用
        # ``wdndds_delete_string_data`` 释放。Python str 走 fallback
        # 分支直接当 raw 喂（3.x 兼容），其它类型保持原样透传。
        fd = get()
        if isinstance(data, str):
            from .types import _import_wdndds_string

            wds = _import_wdndds_string()
            if wds is not None and hasattr(wds, "wdndds_create_string_data_with"):
                sample = wds.wdndds_create_string_data_with(data)
                try:
                    rc = self._raw.write(sample)
                finally:
                    wds.wdndds_delete_string_data(sample)
                check(rc, "DataWriter.write", fd.RETCODE_OK)
                return
        rc = self._raw.write(data)
        check(rc, "DataWriter.write", fd.RETCODE_OK)

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
