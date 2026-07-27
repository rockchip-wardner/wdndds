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
