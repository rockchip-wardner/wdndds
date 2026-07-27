from __future__ import annotations

from typing import Any

from ._fastdds import get


def _new(qos_cls_name: str) -> Any:
    return getattr(get(), qos_cls_name)()


def participant_qos(**overrides: Any) -> Any:
    qos = _new("DomainParticipantQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def topic_qos(**overrides: Any) -> Any:
    qos = _new("TopicQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def publisher_qos(**overrides: Any) -> Any:
    qos = _new("PublisherQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def subscriber_qos(**overrides: Any) -> Any:
    qos = _new("SubscriberQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def datawriter_qos(**overrides: Any) -> Any:
    qos = _new("DataWriterQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def datareader_qos(**overrides: Any) -> Any:
    qos = _new("DataReaderQos")
    for name, value in overrides.items():
        setattr(qos, name, value)
    return qos


def reliable_writer_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datawriter_qos(**overrides)
    qos.reliability().kind = fd.RELIABLE_RELIABILITY_QOS
    return qos


def best_effort_writer_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datawriter_qos(**overrides)
    qos.reliability().kind = fd.BEST_EFFORT_RELIABILITY_QOS
    return qos


def transient_local_writer_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datawriter_qos(**overrides)
    qos.reliability().kind = fd.RELIABLE_RELIABILITY_QOS
    qos.durability().kind = fd.TRANSIENT_LOCAL_DURABILITY_QOS
    return qos


def reliable_reader_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datareader_qos(**overrides)
    qos.reliability().kind = fd.RELIABLE_RELIABILITY_QOS
    return qos


def best_effort_reader_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datareader_qos(**overrides)
    qos.reliability().kind = fd.BEST_EFFORT_RELIABILITY_QOS
    return qos


def transient_local_reader_qos(**overrides: Any) -> Any:
    fd = get()
    qos = datareader_qos(**overrides)
    qos.reliability().kind = fd.RELIABLE_RELIABILITY_QOS
    qos.durability().kind = fd.TRANSIENT_LOCAL_DURABILITY_QOS
    return qos
