[English](README.md) | [简体中文](README_CN.md)
# wdndds - wardner.iot

对 [eProsima Fast DDS](https://www.eprosima.com/en/products/all-products/fast-dds)
Python 绑定的 Python 化封装。该包通过一层轻量、易用的门面（facade）重新导出
这些绑定 —— 上下文管理器、QoS 预设、惰性导入 —— 让你用几行代码就能拉起 DDS 的
participant、publisher 和 subscriber。

`wdndds` 是发行名和导入名；FastDDS 接口位于 `wdndds.dds` 之下。除此之外，
`wdndds` 自身还提供一个小型**能力注册表（capability registry）** —— 同一 DDS
域内的设备可以对外声明自己能做什么，并用一行调用彼此通信。

## wheel 里装了什么

从 v0.2.0 开始，wheel 会在 `wdndds/dds/_native/` 下随包分发**预编译的 eProsima
FastDDS Python 绑定和共享库**：

```
wdndds/dds/_native/
├── fastdds/                  # bundled Python extension (_fastdds_python.so + __init__.py)
├── lib/
│   ├── libfastdds.so.3.6.2.0 + libfastdds.so + libfastdds.so.3.6   # SONAME links
│   └── libfastcdr.so.2.3.6  + libfastcdr.so.2 + libfastcdr.so      # SONAME links
├── _wdndds_string.so         # bundled C++ helper for StringTopicDataType
└── wdndds_string.py          # SWIG-generated Python wrapper
```

`pip install wdndds` **就够了** —— 不需要 apt 依赖，不需要 colcon 编译，不需要
源码树。内置二进制针对以下环境构建：

* aarch64
* Debian 12（glibc 2.36）
* Python 3.11
* libfastdds 3.6.x、libfastcdr 2.3.x

其它平台（x86_64、Ubuntu 22.04 等）必须从源码重新编译；参见
[换平台重编译](#换平台重编译)。

## 安装 `wdndds`

```bash
pip install wdndds
```

## 运行时诊断

```python
from wdndds import dds
print(dds.check_runtime())
# -> {'available': True, 'module': 'fastdds', 'path': '...', 'version': '2.10.x'}
```

如果 `available` 为 `False`，返回的 dict 里会带一个 `error` 字段，给出安装说明。
`bundled` 字段为 `True` 表示加载的是 wheel 内置的绑定（而不是系统安装的
`fastdds`）。

```python
>>> from wdndds import dds
>>> dds.check_runtime()
{
  'available': True,
  'module': 'fastdds',
  'path': '/…/wdndds/dds/_native/fastdds/__init__.py',
  'version': 'unknown',
  'bundled': True,
}
```

## 为什么需要内置的 C++ 辅助库（`WdnddsStringTopicDataType`）？

官方 `eProsima Fast-DDS Python` 绑定**没有内置字符串类型的 `TopicDataType`**。
上游的 `eprosima::fastdds::dds::StringTopicDataType` 只有 C++ 版本，SWIG 不会
自动生成对应的封装。即便我们能在 Python 里定义一个，SWIG 生成的
`fastdds.TypeSupport(TopicDataType *)` 重载也会拒绝任何不是 SWIG 生成的
`TopicDataType` 子类的对象。

折中办法：由同级仓库（`../wdndds_cxx/`）定义
`wdndds::WdnddsStringTopicDataType : public eprosima::fastdds::dds::TopicDataType`，
并导出 `wdndds_register_string_type(participant, alias)` —— `TypeSupport` 的包装
和注册都在 C++ 侧完成。SWIG 会自动生成 Python 封装（`wdndds_string.py`）。

CDR 编码很简单：`u32 小端长度 + UTF-8 载荷`（不带 NUL 结束符 —— FastDDS CDR
不写）。字符串上限 4 MiB，用于限制恶意对端占用的内存。

```python
import wdndds.dds
from wdndds.dds.domain import Participant

hub = Participant(domain_id=0)
type_name = hub.register_string_type("registry")   # uses bundled C++ helper
print(type_name)                                     # wdndds::string::registry
```

## 换平台重编译

内置产物与平台绑定。要为其它目标（x86_64、Ubuntu 22.04、Python 3.10 等）
重新编译：

```bash
# 1. host build deps
sudo apt install -y libasio-dev libtinyxml2-dev libfastrtps-dev swig cmake g++ python3-dev
.venv/bin/pip install -U colcon-common-extensions vcstool

# 2. all-in-one build (uses $FASTDDDS_WS=/tmp/fastdds_ws by default;
#    override the env var to share a workspace across builds)
./build_native.sh [<platform-tag>]

# 3. reinstall
.venv/bin/pip install -e . --force-reinstall --no-deps
```

在小板子上完整编译约需 6 分钟。磁盘空间要求：5-10 GB（colcon 的中间产物）。

## 修改 `idl/compute.idl`

IDL 生成的 ``computePubSubTypes.cxx`` + ``computeTypeObjectSupport.cxx``
（以及配套的 ``.hpp`` / ``.ipp`` 头文件）与 ``idl/compute.idl`` 一起提交，
这样构建 wheel 时永远不需要 ``fastddsgen``。

改动 ``idl/compute.idl`` 后，用内置的 fastddsgen（一个调用内置
``fastddsgen.jar`` 的 Java 包装脚本）重新生成：

```bash
# -replace: overwrite existing files
# -d .:    output into the current dir, i.e. alongside compute.idl
#          (use -d idl  by mistake and you get idl/idl/compute.* — wrong)
native/bin/fastddsgen -replace -d . idl/compute.idl

git add idl/compute*.{cxx,hpp,ipp}
git commit -m 'feat: regenerate IDL after compute.idl change'
```

需要 PATH 上有 JDK 17+（任意较新的 OpenJDK 均可）。内置的
``native/share/fastddsgen/java/fastddsgen.jar``（18 MB）是 eProsima 官方的
FastDDS-Gen 4.3.0 构建 —— 不要替换成别的版本，除非同时重新生成已提交的
``idl/*.cxx`` 文件（那些文件与该生成器版本绑定）。

## 网络要求

DDS 发现使用 **UDP 组播**（`239.255.0.1`，端口从 7400 起）。两端必须位于同一个
开启了组播转发的二层网络中。可以用下面的命令验证：

```bash
tcpdump -n -i any 'udp port 7400 or igmp'   # should show SPDP participant traffic
```

在没有组播的沙箱环境里（Docker 未加 `--network=host`、受限的 CNI 插件），
初始化 Discovery Participant 时可能卡在 `create_writer` / `create_reader` ——
改用 `FASTRTPS_DEFAULT_PROFILES_FILE`，让 FastDDS 走一个仅限 localhost 的
XML 配置。

## 能力注册表（概览）

同一 DDS 域内的设备通过注册一个名字和一组能力实例来对外声明自己。其它设备
扫描、发现它们，然后调用其能力。每次网络调用都返回一个
`concurrent.futures.Future`。

```python
import wdndds
from wdndds import HWVideoInput, SWVideoQuery, HWAudioOutput

with wdndds.connect():
    wdndds.register("Gate1Office", [
        HWVideoInput("大门正门",   callback=on_frame),
        SWVideoQuery("大门正门",   callback=on_query),
        HWAudioOutput("大门喇叭", callback=on_play),
    ])
```

发现是推送式的：注册一个回调，由用户自己维护的字典就会保持同步。

```python
import wdndds

devices: dict[str, wdndds.Device] = {}

def on_event(device: wdndds.Device, event: wdndds.Event) -> None:
    if event.kind == "added":
        devices[device.name] = device
        print(f"+ {device.name} caps={[c.__name__ for c in device.capabilities]}")
    elif event.kind == "removed":
        devices.pop(device.name, None)

with wdndds.connect():
    wdndds.start_scan(on_event)
    # ... when a Gate1Office appears in `devices`:
    gate1 = devices["Gate1Office"]
    fut = gate1.SWVideoQuery["大门正门"]({"q": "what do you see?"})
    print(fut.result(timeout=2.0))
```

`removed` 事件是尽力而为的：它依赖 FastDDS 内置的 participant 存活检测。如果
你的绑定没有 listener API，就只会触发 `added` 事件，字典需要自己维护。

### 预置能力

能力分为两类：

* **HW**（`hwcap_*` 前缀）—— 物理 I/O：摄像头、麦克风、扬声器、HDMI 输出。
  继承 `HWCapability`（顶层不导出）。
* **SW**（`swcap_*` 前缀）—— 算法能力：视频问答 / 描述 / 分析，音频问答 /
  分析 / TTS，文本对话。继承 `SWCapability`（顶层不导出）。

| 类 | DDS 类型 | 分类 | 默认行为（无回调） |
|---|---|---|---|
| `HWVideoInput`    | `hwcap_video_input`     | HW | drops frames |
| `SWVideoQuery`    | `swcap_video_query`     | SW | `{"answer": "unconfigured"}` |
| `SWVideoDescribe` | `swcap_video_describe`  | SW | `{"description": "unconfigured"}` |
| `SWVideoAnalyse`  | `swcap_video_analyse`   | SW | `{"result": None, "reason": ...}` |
| `HWAudioInput`    | `hwcap_audio_input`     | HW | drops samples |
| `HWAudioOutput`   | `hwcap_audio_output`    | HW | `{"played": False, ...}` |
| `SWAudioQuery`    | `swcap_audio_query`     | SW | `{"answer": "unconfigured"}` |
| `SWAudioAnalyse`  | `swcap_audio_analyse`   | SW | `{"result": None}` |
| `SWAudioTTS`      | `swcap_audio_tts`       | SW | `{"synthesized": False, "audio": None, ...}` |
| `SWTextChat`      | `swcap_text_chat`       | SW | `{"reply": "unconfigured", "messages": []}` |
| `HWHDMIDisplay`   | `hwcap_hdmi_display`    | HW | `{"displayed": False, ...}` |

要新增自己的能力，继承 `Capability` —— 设置 `capability_type`（必须以 `hwcap_`
或 `swcap_` 开头，取决于走 `HWCapability` 还是 `SWCapability`），并重写
`_default_handle`。

### 回调约定

```python
def my_handler(payload, reply):
    # payload is whatever the caller passed to cap(...)
    # call reply(value) exactly once with the result you want the caller to see
    reply({"echo": payload})
```

远程调用时，同一个回调会在消息到达能力 topic 时由 wdndds 的后台线程执行；
本地调用时（对刚构造出来的实例调 `cap(...)`），它同步执行，`cap(...)` 直接
返回回复值。

### 端到端示例

每个脚本实例**就是**一台设备：它注册自己的能力，同时扫描其它设备。在同一台
主机（或同一 DDS 域内的任意主机）上用不同的 `--name` 跑两个实例，它们会互相
发现并互发 `SWVideoQuery` 调用：

```bash
# Terminal A
python samples/register_and_find.py --name Gate1Office

# Terminal B
python samples/register_and_find.py --name Gate2Office
```

## 底层 DDS 门面（`wdndds.dds`）

```python
from wdndds import dds

with dds.Participant(domain_id=0) as participant:
    topic = participant.create_topic("Hello", "HelloWorld")
    with participant.create_publisher() as pub:
        with pub.create_writer(topic) as writer:
            writer.write({"message": "hi", "index": 0})
```

`Reader` 支持按样本迭代：

```python
with participant.create_subscriber() as sub:
    with sub.create_reader(topic) as reader:
        for data, info in reader:
            print(info.valid_data, data)
```

## QoS 预设

```python
from wdndds import dds

writer_qos = dds.reliable_writer_qos()
reader_qos = dds.best_effort_reader_qos()
```

能力注册表在注册表 topic 上使用 `transient_local_*` QoS，在每个能力的 topic 和
回复 topic 上使用 `reliable_*`。

## 从源码构建

```bash
python -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple build pytest ruff
.venv/bin/python -m build
```

这会生成 `dist/wdndds-<version>-py3-none-any.whl` 以及对应的 sdist。

## 项目结构

```
wdndds/
├── pyproject.toml
├── build_native.sh       # rebuilds the bundled FastDDS Python bindings + C++ helper
├── src/wdndds/
│   ├── __init__.py       # public API: Capability classes, connect, register,
│   │                     #             start_scan, stop_scan, Device, Event
│   ├── _version.py
│   ├── py.typed
│   ├── capabilities.py   # Capability base + HWCapability/SWCapability + 11 predefined subclasses
│   ├── event.py          # Event dataclass
│   ├── registry.py       # Hub, Device, CapabilityNamespace, scan/invoke
│   └── dds/
│       ├── __init__.py    # re-exports the public surface
│       ├── _fastdds.py    # bootstrap: preloads libfastcdr + libfastdds, then
│       │                  # imports the bundled `fastdds` Python module
│       ├── exceptions.py
│       ├── qos.py         # QoS factories and presets
│       ├── types.py       # register_string_type via bundled C++ helper
│       ├── domain.py      # Participant
│       ├── topic.py       # Topic
│       ├── publisher.py   # Publisher, Writer
│       ├── subscriber.py  # Subscriber, Reader
│       └── _native/       # wheel-shipped native payloads (FastDDS + helper):
│           ├── fastdds/__init__.py + _fastdds_python.so
│           ├── lib/libfastdds.so.3.6[.2.0] + libfastcdr.so.2[.3.6]
│           ├── _wdndds_string.so   # bundled WdnddsStringTopicDataType SWIG ext
│           └── wdndds_string.py    # SWIG-generated Python wrapper
└── ../wdndds_cxx/        # sibling C++ project (built by build_native.sh):
    ├── CMakeLists.txt
    ├── include/WdnddsString.hpp
    ├── src/WdnddsString.cpp + WdnddsString.i
    └── build/generated/   # → wdndds/dds/_native/_wdndds_string.so + wdndds_string.py

samples/
└── register_and_find.py   # provider / caller two-role end-to-end sample
```

## 第三方组件与许可

本仓随仓再分发 `native/` 下的第三方预编译产物：

| 组件 | 版本 | 许可 | 位置 |
|---|---|---|---|
| Fast DDS / Fast CDR（eProsima） | 3.6.2 / 2.3.6 | Apache-2.0 | `native/include/fastdds/`、`native/include/fastcdr/`、`native/lib/libfastdds.so.3.6.2.0`、`native/lib/libfastcdr.so.2.3.6` —— 上游许可原样随附在 [`native/LICENSE`](native/LICENSE) |
| foonathan-memory | 0.7.4 | Boost Software License 1.0 | `native/include/foonathan/`、`native/lib/libfoonathan_memory-0.7.4.a` —— 许可文件在 `native/share/foonathan_memory/LICENSE` |

两者都是宽松许可，与 Apache-2.0 兼容。Fast DDS 上游**没有 NOTICE 文件**，
所以没有需要转带的署名声明（Apache-2.0 §4(d) 只在上游作品带 NOTICE 时才适用）。

## License

Apache-2.0。见 [LICENSE](LICENSE)。
