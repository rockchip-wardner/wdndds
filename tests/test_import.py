from types import SimpleNamespace

import pytest


def test_version_is_string():
    import wdiot

    assert isinstance(wdiot.__version__, str)
    assert wdiot.__version__


def test_dds_module_surface():
    import wdiot.dds as dds

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
    from wdiot.dds import _fastdds

    _fastdds._fastdds = None

    real_import = __import__("builtins").__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fastdds":
            raise ImportError("simulated missing fastdds")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ImportError, match=r"wdiot\.dds requires"):
        _fastdds.get()


def test_exceptions():
    from wdiot.dds.exceptions import ReturnCodeError

    err = ReturnCodeError(7, "boom")
    assert err.code == 7
    assert "7" in str(err)
    assert "boom" in str(err)


def test_writer_attribute_isolation():
    from wdiot.dds.publisher import Writer

    class FakeRaw:
        def write(self, data):
            return 0

    w = Writer(FakeRaw(), owner=None, owned=False)
    assert w.raw is w._raw


def test_reader_iter_protocol():
    from wdiot.dds.subscriber import Reader

    class FakeRaw:
        def __init__(self, payload):
            self.payload = payload

        def take(self, _n):
            return self.payload

    samples = [("a", object()), ("b", object())]
    r = Reader(FakeRaw(samples), owner=None, owned=False)
    assert list(iter(r)) == samples


def test_check_runtime_reports_unavailable():
    from wdiot import dds
    from wdiot.dds import _fastdds

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
    assert "wdiot.dds requires" in info["error"]
    assert "apt install" in info["error"]


def test_check_runtime_reports_available():
    from wdiot import dds
    from wdiot.dds import _fastdds

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
    from wdiot.dds import _fastdds, types

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

    assert tname == "wdiot::string::demo"
    assert len(fp.registered) == 1
    assert fp.registered[0].name == "wdiot::string::demo"


def test_register_string_type_unique_per_alias():
    from wdiot.dds import _fastdds, types

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
    assert a == "wdiot::string::alpha"
    assert b == "wdiot::string::beta"
    assert len(fp.registered) == 2


def test_samples_compile():
    import py_compile

    py_compile.compile("samples/register_and_find.py", doraise=True)


def test_event_dataclass():
    from wdiot import Event

    e = Event(kind="added")
    assert e.kind == "added"
    assert e.capability is None
    e2 = Event(kind="removed", capability=None)
    assert e2.kind == "removed"
    assert e2.capability is None


def test_capability_default_handle():
    from wdiot import HWAudioOutput, SWVideoQuery

    captured: list = []
    cam = SWVideoQuery("cam1")
    cam._default_handle({"q": "hi"}, captured.append)
    assert captured == [{"answer": "unconfigured"}]

    spk = HWAudioOutput("spk1")
    captured.clear()
    spk._default_handle({"x": 1}, captured.append)
    assert captured == [{"played": False, "reason": "no speaker handler"}]


def test_capability_local_call_returns_reply():
    from wdiot import HWVideoInput

    def cb(payload, reply):
        reply({"ok": True, "echoed": payload})

    cap = HWVideoInput("cam1", callback=cb)
    assert cap({"hello": "world"}) == {"ok": True, "echoed": {"hello": "world"}}


def test_capability_local_call_uses_default_when_no_callback():
    from wdiot import SWAudioAnalyse

    cap = SWAudioAnalyse("a1")
    out = cap({"any": "payload"})
    assert out == {"result": None}


def test_register_top_level_signature():
    import inspect

    import wdiot

    sig = inspect.signature(wdiot.register)
    assert list(sig.parameters.keys()) == ["name", "capabilities"]


def test_no_find_search_apis():
    import wdiot

    for name in ("find", "search"):
        assert not hasattr(wdiot, name), f"wdiot.{name} should not exist"
    for name in ("start_scan", "stop_scan", "register", "connect", "Event", "Device"):
        assert hasattr(wdiot, name), f"wdiot.{name} should exist"


def test_capability_classes_share_base():
    from wdiot import (
        HWAudioInput,
        HWAudioOutput,
        HWHDMIDisplay,
        HWVideoInput,
        SWAudioAnalyse,
        SWAudioQuery,
        SWAudioTTS,
        SWTextChat,
        SWVideoAnalyse,
        SWVideoDescribe,
        SWVideoQuery,
    )
    from wdiot.capabilities import Capability, HWCapability, SWCapability

    all_leaf = (
        HWVideoInput, SWVideoQuery, SWVideoDescribe, SWVideoAnalyse,
        HWAudioInput, HWAudioOutput, SWAudioQuery, SWAudioAnalyse,
        SWAudioTTS, SWTextChat, HWHDMIDisplay,
    )
    for cls in all_leaf:
        assert issubclass(cls, Capability)
        assert cls.capability_type, f"{cls.__name__} missing capability_type"
    types = {c.capability_type for c in all_leaf}
    assert len(types) == 11

    # category 归属：HW 类必须在 HWCapability 下、SW 类必须在 SWCapability 下。
    for hw_cls in (HWVideoInput, HWAudioInput, HWAudioOutput, HWHDMIDisplay):
        assert issubclass(hw_cls, HWCapability)
        assert hw_cls.category == "hw"
        assert hw_cls.capability_type.startswith("hwcap_")
    for sw_cls in (
        SWVideoQuery, SWVideoDescribe, SWVideoAnalyse,
        SWAudioQuery, SWAudioAnalyse, SWAudioTTS, SWTextChat,
    ):
        assert issubclass(sw_cls, SWCapability)
        assert sw_cls.category == "sw"
        assert sw_cls.capability_type.startswith("swcap_")


def test_swaudiotts_default_handle():
    from wdiot import SWAudioTTS

    captured: list = []
    cap = SWAudioTTS("tts1")
    cap._default_handle({"text": "hi"}, captured.append)
    assert captured == [{"synthesized": False, "audio": None, "reason": "no tts handler"}]


def test_swvideoanalyse_default_handle():
    from wdiot import SWVideoAnalyse

    captured: list = []
    cap = SWVideoAnalyse("va1")
    cap._default_handle({"frame": object()}, captured.append)
    assert captured == [{"result": None, "reason": "no video analyser"}]


def test_swtextchat_default_handle():
    from wdiot import SWTextChat

    captured: list = []
    cap = SWTextChat("chat1")
    cap._default_handle({"messages": []}, captured.append)
    assert captured == [{"reply": "unconfigured", "messages": []}]


def test_device_update_caps():
    from wdiot import HWAudioOutput, HWVideoInput
    from wdiot.registry import Device

    class FakeHub:
        pass

    dev = Device(name="test", hub=FakeHub())
    dev._update_caps([
        {"type": "hwcap_video_input", "name": "cam1"},
        {"type": "hwcap_audio_output", "name": "spk1"},
    ])
    assert HWVideoInput in dev.caps
    assert HWAudioOutput in dev.caps
    assert dev.caps[HWVideoInput]["cam1"].name == "cam1"
    assert dev.caps[HWAudioOutput]["spk1"].name == "spk1"


def test_device_namespace_lookup_attribute_and_subscript():
    from wdiot import HWVideoInput
    from wdiot.registry import Device, _CapabilityNamespace

    class FakeHub:
        pass

    dev = Device(name="test", hub=FakeHub())
    ns_attr = dev.HWVideoInput
    ns_getitem = dev[HWVideoInput]
    assert isinstance(ns_attr, _CapabilityNamespace)
    assert isinstance(ns_getitem, _CapabilityNamespace)
    assert ns_attr.cls is HWVideoInput

    cam = ns_attr["大门正门"]
    assert isinstance(cam, HWVideoInput)
    assert cam.name == "大门正门"
    assert cam._target is dev


def test_device_getattr_raises_for_unknown():
    from wdiot.registry import Device

    class FakeHub:
        pass

    dev = Device(name="test", hub=FakeHub())
    with pytest.raises(AttributeError):
        _ = dev.NotARealCapabilityClass


def test_device_getitem_rejects_non_type():
    from wdiot.registry import Device

    class FakeHub:
        pass

    dev = Device(name="test", hub=FakeHub())
    with pytest.raises(TypeError):
        dev["not a class"]


def test_register_requires_connect(monkeypatch):
    from wdiot import HWVideoInput
    from wdiot import registry as _reg

    monkeypatch.setattr(_reg, "_hub", None)
    with pytest.raises(RuntimeError, match=r"not connected"):
        _reg.register("X", [HWVideoInput("y")])
