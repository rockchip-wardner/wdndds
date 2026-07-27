from types import SimpleNamespace

import pytest


def test_version_is_string():
    import pyiot

    assert isinstance(pyiot.__version__, str)
    assert pyiot.__version__


def test_dds_module_surface():
    import pyiot.dds as dds

    expected = {
        "Participant",
        "Publisher",
        "Subscriber",
        "Topic",
        "Writer",
        "Reader",
        "participant_qos",
        "topic_qos",
        "publisher_qos",
        "subscriber_qos",
        "datawriter_qos",
        "datareader_qos",
        "reliable_writer_qos",
        "best_effort_writer_qos",
        "reliable_reader_qos",
        "best_effort_reader_qos",
        "transient_local_writer_qos",
        "transient_local_reader_qos",
        "DDSError",
        "ReturnCodeError",
    }
    assert expected.issubset(set(dir(dds)))


def test_lazy_friendly_error_when_fastdds_missing(monkeypatch):
    from pyiot.dds import _fastdds

    _fastdds._fastdds = None

    real_import = __import__("builtins").__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fastdds":
            raise ImportError("simulated missing fastdds")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ImportError, match=r"pyiot\.dds requires"):
        _fastdds.get()


def test_exceptions():
    from pyiot.dds.exceptions import ReturnCodeError

    err = ReturnCodeError(7, "boom")
    assert err.code == 7
    assert "7" in str(err)
    assert "boom" in str(err)


def test_writer_attribute_isolation():
    from pyiot.dds.publisher import Writer

    class FakeRaw:
        def write(self, data):
            return 0

    w = Writer(FakeRaw(), owner=None, owned=False)
    assert w.raw is w._raw


def test_reader_iter_protocol():
    from pyiot.dds.subscriber import Reader

    class FakeRaw:
        def __init__(self, payload):
            self.payload = payload

        def take(self, _n):
            return self.payload

    samples = [("a", object()), ("b", object())]
    r = Reader(FakeRaw(samples), owner=None, owned=False)
    assert list(iter(r)) == samples


def test_check_runtime_reports_unavailable():
    from pyiot import dds
    from pyiot.dds import _fastdds

    _fastdds._fastdds = None

    real_import = __import__("builtins").__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fastdds":
            raise ImportError("simulated missing fastdds")
        return real_import(name, globals, locals, fromlist, level)

    import builtins

    saved = builtins.__import__
    builtins.__import__ = fake_import
    try:
        info = dds.check_runtime()
    finally:
        builtins.__import__ = saved

    assert info["available"] is False
    assert "pyiot.dds requires" in info["error"]
    assert "apt install" in info["error"]


def test_check_runtime_reports_available():
    from pyiot import dds
    from pyiot.dds import _fastdds

    _fastdds._fastdds = None
    fake = type(
        "fastdds",
        (),
        {"__file__": "/fake/fastdds.py", "__version__": "9.9.9"},
    )
    _fastdds._fastdds = fake
    try:
        info = dds.check_runtime()
    finally:
        _fastdds._fastdds = None

    assert info == {
        "available": True,
        "module": "fastdds",
        "path": "/fake/fastdds.py",
        "version": "9.9.9",
    }


def test_register_string_type_helper():
    from pyiot.dds import _fastdds, types

    class FakeType:
        def __init__(self):
            self.name = None

        def set_name(self, n):
            self.name = n

    class FakeParticipant:
        def __init__(self):
            self.registered = []

        def register_type(self, ts):
            self.registered.append(ts)

    saved = _fastdds._fastdds
    _fastdds._fastdds = SimpleNamespace(StringTopicDataType=FakeType)
    try:
        fp = FakeParticipant()
        tname = types.register_string_type(fp, "demo")
    finally:
        _fastdds._fastdds = saved

    assert tname == "pyiot::string::demo"
    assert len(fp.registered) == 1
    assert fp.registered[0].name == "pyiot::string::demo"


def test_register_string_type_unique_per_alias():
    from pyiot.dds import _fastdds, types

    class FakeType:
        def __init__(self):
            self.name = None

        def set_name(self, n):
            self.name = n

    class FakeParticipant:
        def __init__(self):
            self.registered = []

        def register_type(self, ts):
            self.registered.append(ts)

    saved = _fastdds._fastdds
    _fastdds._fastdds = SimpleNamespace(StringTopicDataType=FakeType)
    try:
        fp = FakeParticipant()
        a = types.register_string_type(fp, "alpha")
        b = types.register_string_type(fp, "beta")
    finally:
        _fastdds._fastdds = saved

    assert a != b
    assert a == "pyiot::string::alpha"
    assert b == "pyiot::string::beta"
    assert len(fp.registered) == 2


def test_samples_compile():
    import py_compile

    for name in ("common", "register", "discover", "__main__"):
        py_compile.compile(f"samples/registry/{name}.py", doraise=True)
