from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar

if TYPE_CHECKING:
    from .registry import Device


Callback = Callable[[Any, Callable[[Any], None]], None]


class Capability:
    """Base class for every capability a wdndds device can advertise.

    Subclasses must set :attr:`capability_type` (the string used on the DDS
    wire). They may override :meth:`_default_handle` to provide a sensible
    response when the user registers the capability without a callback.

    Construction
    ------------
    Use ``name`` for the per-instance label (e.g. ``"大门正门"``) and
    ``callback`` for the request handler:

        cam = HWVideoInput("大门正门", callback=on_frame)

    The callback receives ``(payload, reply)``. It must call ``reply(value)``
    exactly once — that's how the caller's ``Future`` completes.

    When the same class is used to address a remote instance
    (``HWVideoInput("大门正门", target=remote_device)``) ``__call__`` becomes
    a remote invocation that returns a ``concurrent.futures.Future``.

    Categories
    ----------
    Concrete capabilities should subclass one of the two intermediate bases
    — :class:`HWCapability` (physical I/O) or :class:`SWCapability`
    (algorithmic / query / analysis) — instead of :class:`Capability`
    directly. ``capability_type`` strings must start with ``hwcap_`` or
    ``swcap_`` accordingly.
    """

    capability_type: ClassVar[str] = ""

    def __init__(
        self,
        name: str,
        callback: Callback | None = None,
        *,
        target: Device | None = None,
    ) -> None:
        self.name = name
        self._user_callback = callback
        self._target = target

    def __call__(self, payload: Any, *, timeout: float | None = None):
        if self._target is not None:
            return self._target._hub.invoke(
                target_device=self._target.name,
                cap_type=self.capability_type,
                instance_name=self.name,
                payload=payload,
                timeout=timeout,
            )
        result: dict[str, Any] = {}

        def reply(value: Any) -> None:
            result["v"] = value

        self._handle(payload, reply)
        return result.get("v")

    def _handle(self, payload: Any, reply: Callable[[Any], None]) -> None:
        if self._user_callback is not None:
            self._user_callback(payload, reply)
        else:
            self._default_handle(payload, reply)

    def _default_handle(self, payload: Any, reply: Callable[[Any], None]) -> None:
        reply({"ok": False, "error": f"no handler for {self.capability_type}/{self.name}"})

    def __repr__(self) -> str:
        side = "remote" if self._target is not None else "local"
        return f"<{type(self).__name__} {self.name!r} ({side})>"


class HWCapability(Capability):
    """硬件能力基类 — 物理 I/O 设备（摄像头、麦克风、扬声器、HDMI 等）。

    ``capability_type`` 必须以 ``hwcap_`` 开头，用于在 DDS topic
    ``wdndds/capability/<type>`` 上区分硬件通路。

    该基类不直接对外导出，外部代码请使用具体的叶子类
    （``HWVideoInput`` / ``HWAudioInput`` / ``HWAudioOutput`` /
    ``HWHDMIDisplay``）。
    """

    category: ClassVar[str] = "hw"


class SWCapability(Capability):
    """算法/软件能力基类 — 在硬件流之上做的查询、描述、分析等。

    ``capability_type`` 必须以 ``swcap_`` 开头。SW 能力通常由
    一个具体的模型/LLM session 实现，与硬件解耦。

    该基类不直接对外导出，外部代码请使用具体的叶子类
    （``SWVideoQuery`` / ``SWVideoDescribe`` / ``SWAudioQuery`` /
    ``SWAudioAnalyse``）。
    """

    category: ClassVar[str] = "sw"


class HWVideoInput(HWCapability):
    """硬件能力：摄像头产生的视频流（caller 主动 push）。"""

    capability_type = "hwcap_video_input"

    def _default_handle(self, payload, reply):
        reply({"ok": False, "reason": "no camera handler", "dropped": True})


class SWVideoQuery(SWCapability):
    """算法能力：向视频源发起查询（如"你看到了什么？"），通常由 VLM 推理。"""

    capability_type = "swcap_video_query"

    def _default_handle(self, payload, reply):
        reply({"answer": "unconfigured"})


class SWVideoDescribe(SWCapability):
    """算法能力：让视频源描述当前画面（自由文本 / 结构化）。"""

    capability_type = "swcap_video_describe"

    def _default_handle(self, payload, reply):
        reply({"description": "unconfigured"})


class HWAudioInput(HWCapability):
    """硬件能力：麦克风产生的音频流。"""

    capability_type = "hwcap_audio_input"

    def _default_handle(self, payload, reply):
        reply({"ok": False, "reason": "no mic handler", "dropped": True})


class HWAudioOutput(HWCapability):
    """硬件能力：扬声器播放音频。"""

    capability_type = "hwcap_audio_output"

    def _default_handle(self, payload, reply):
        reply({"played": False, "reason": "no speaker handler"})


class SWAudioQuery(SWCapability):
    """算法能力：音频源查询（自由文本问答）。"""

    capability_type = "swcap_audio_query"

    def _default_handle(self, payload, reply):
        reply({"answer": "unconfigured"})


class SWAudioAnalyse(SWCapability):
    """算法能力：在音频流上做分析（level / VAD / 分类等）。"""

    capability_type = "swcap_audio_analyse"

    def _default_handle(self, payload, reply):
        reply({"result": None})


class SWVideoAnalyse(SWCapability):
    """算法能力：视频结构化分析（检测 / 分类 / 统计等）。

    区别于 :class:`SWVideoQuery`（自由问答）和 :class:`SWVideoDescribe`
    （自由文本描述），本能力面向有结构的输出（bbox / 标签 / 计数等）。
    """

    capability_type = "swcap_video_analyse"

    def _default_handle(self, payload, reply):
        reply({"result": None, "reason": "no video analyser"})


class SWAudioTTS(SWCapability):
    """算法能力：把文本合成音频波形（不含扬声器回放，回放由 :class:`HWAudioOutput` 负责）。

    一般与 :class:`HWAudioOutput` 配对使用 —— SW 侧负责把文本生成
    音频数据，HW 侧负责把音频数据送进扬声器。
    """

    capability_type = "swcap_audio_tts"

    def _default_handle(self, payload, reply):
        reply({"synthesized": False, "audio": None, "reason": "no tts handler"})


class SWTextChat(SWCapability):
    """算法能力：纯文本对话会话（LLM chat），modality = text。

    不绑定音/视频流 —— 输入和输出都是纯文本，由 LLM session 完成。
    用于"通用 chat / 闲聊 / 问答 / 工具调用"等不依赖多媒体的场景。
    """

    capability_type = "swcap_text_chat"

    def _default_handle(self, payload, reply):
        reply({"reply": "unconfigured", "messages": []})


class HWHDMIDisplay(HWCapability):
    """硬件能力：HDMI 显示输出 sink。"""

    capability_type = "hwcap_hdmi_display"

    def _default_handle(self, payload, reply):
        reply({"displayed": False, "reason": "no display handler"})