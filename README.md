# wdndds - wardner.iot

Pythonic wrapper around [eProsima Fast DDS](https://www.eprosima.com/en/products/all-products/fast-dds)
Python bindings. The package re-exports the bindings through a small, ergonomic
facade — context managers, QoS presets, lazy import — so you can spin up a DDS
participant, publisher, and subscriber in a few lines.

`wdndds` is the distribution / import name; the FastDDS surface lives under
`wdndds.dds`. On top of that, `wdndds` itself exposes a small **capability
registry** so devices on the same DDS domain can advertise what they do and
call each other with one-liners.

## Prerequisites

`wdndds` has **no PyPI dependencies** — it is a pure-Python facade. The native
eProsima Fast DDS Python bindings (`fastdds` / `fastdds_python`) are NOT on
PyPI, so you must install them via your OS or build from source. Choose one:

**Debian / Ubuntu (recommended)**

```bash
sudo apt install -y libasio-dev libtinyxml2-dev libfastdds-dev fastdds-python3
```

**Build from source** (see [docs](https://fast-dds.docs.eprosima.com/en/latest/fastdds/python_bindings/python_bindings.html))

```bash
pip install -U colcon-common-extensions vcstool
mkdir -p fastdds_ws/src && cd fastdds_ws
wget https://raw.githubusercontent.com/eProsima/Fast-DDS-Python/main/fastdds_python.repos
vcs import src < fastdds_python.repos
colcon build
```

Verify the bindings are importable:

```bash
python -c 'import fastdds; print(fastdds.__file__)'
```

If that prints a path, you're good.

## Install `wdndds`

```bash
pip install wdndds
```

## Diagnose at runtime

```python
from wdndds import dds
print(dds.check_runtime())
# -> {'available': True, 'module': 'fastdds', 'path': '...', 'version': '2.10.x'}
```

If `available` is `False`, the returned dict carries an `error` field with
install instructions.

## Capability registry (high-level)

Devices on the same DDS domain advertise themselves by registering a name and
a list of capability instances. Other devices scan, discover them, and invoke
their capabilities. Every network invocation returns a
`concurrent.futures.Future`.

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

Discovery is push-based: register a callback and the user-owned dictionary is
kept in sync.

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

`removed` events are best-effort: they rely on FastDDS built-in participant
liveliness. If the listener API isn't available on your binding, only `added`
events fire and you manage your dictionary yourself.

### Predefined capabilities

Capabilities are split into two categories:

* **HW** (`hwcap_*` prefix) — physical I/O: cameras, microphones, speakers,
  HDMI sinks. Subclass `HWCapability` (not exported at the top level).
* **SW** (`swcap_*` prefix) — algorithmic: video query / describe /
  analyse, audio query / analyse / TTS, text chat. Subclass
  `SWCapability` (not exported at the top level).

| Class | DDS type | Category | Default (no callback) |
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

Subclass `Capability` to add your own — set `capability_type` (must start
with `hwcap_` or `swcap_` depending on whether you go via `HWCapability`
or `SWCapability`) and override `_default_handle`.

### Callback contract

```python
def my_handler(payload, reply):
    # payload is whatever the caller passed to cap(...)
    # call reply(value) exactly once with the result you want the caller to see
    reply({"echo": payload})
```

For remote invocations the same callback runs on a wdndds background thread
when a message arrives on the capability topic; for local invocations
(`cap(...)` on a freshly constructed instance) it runs synchronously and
`cap(...)` returns the reply value.

### End-to-end sample

Each script instance IS one device: it registers its own capabilities and
also scans for other devices. Run two instances with different `--name`
values on the same host (or any host on the same DDS domain) and they will
discover each other and trade `SWVideoQuery` calls:

```bash
# Terminal A
python samples/register_and_find.py --name Gate1Office

# Terminal B
python samples/register_and_find.py --name Gate2Office
```

## Low-level DDS facade (`wdndds.dds`)

```python
from wdndds import dds

with dds.Participant(domain_id=0) as participant:
    topic = participant.create_topic("Hello", "HelloWorld")
    with participant.create_publisher() as pub:
        with pub.create_writer(topic) as writer:
            writer.write({"message": "hi", "index": 0})
```

`Reader` supports iteration over samples:

```python
with participant.create_subscriber() as sub:
    with sub.create_reader(topic) as reader:
        for data, info in reader:
            print(info.valid_data, data)
```

## QoS presets

```python
from wdndds import dds

writer_qos = dds.reliable_writer_qos()
reader_qos = dds.best_effort_reader_qos()
```

The capability registry uses `transient_local_*` QoS on the registry topic and
`reliable_*` on the per-capability topics and reply topics.

## Build from source

```bash
python -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple build pytest ruff
.venv/bin/python -m build
```

This produces `dist/wdndds-<version>-py3-none-any.whl` and the matching sdist.

## Project layout

```
src/wdndds/
├── __init__.py        # public API: Capability classes, connect, register,
│                      #             start_scan, stop_scan, Device, Event
├── _version.py
├── py.typed
├── capabilities.py    # Capability base + HWCapability/SWCapability + 8 predefined subclasses
├── event.py           # Event dataclass
├── registry.py        # Hub, Device, CapabilityNamespace, scan/invoke
└── dds/
    ├── __init__.py     # re-exports the public surface
    ├── _fastdds.py     # lazy loader for the native `fastdds` module
    ├── exceptions.py
    ├── qos.py          # QoS factories and presets
    ├── types.py        # register_string_type + topic introspection
    ├── domain.py       # Participant
    ├── topic.py        # Topic
    ├── publisher.py    # Publisher, Writer
    └── subscriber.py   # Subscriber, Reader

samples/
└── register_and_find.py   # provider / caller two-role end-to-end sample
```

## WeChat 机器人 (`wdndds.Wechat`)

A small synchronous wrapper around the [`wechatbot-sdk`](https://pypi.org/project/wechatbot-sdk/)
async client. It exposes QR-code login, long-poll message reception, and
text/media send behind a thread-safe API — drop it into any service
process (FastAPI handler, cron script, debugging CLI) without managing an
asyncio loop yourself.

```bash
pip install wdndds            # wechatbot-sdk is a required dependency
```

```python
import wdndds

wc = wdndds.Wechat()          # spins up a private asyncio loop on a daemon thread
print(wc.check_runtime())    # {'available': True, 'module': 'wechatbot', ...}

wc.start_login(force=True)   # schedules bot.login() on the loop; returns immediately
# poll wc.login_status() / wc.wait_qr_url() until wc.started == True
print(wc.login_status())     # {'state': 'awaiting_scan', 'qr_url': 'weixin://...', ...}

@wc.register_message_handler
def on_message(msg):
    print(f"[{msg.type}] {msg.user_id}: {msg.text}")
    wc.send_text(msg.user_id, f"Echo: {msg.text}")

# Backed by ~/.wechatbot/credentials.json by default — set
# WDNDDS_WECHAT_CRED_PATH to override.
wc.logout()                  # clears on-disk creds + stops the long-poll
wc.close()                   # tears down the daemon loop
```

Credentials
-----------

Stored on disk under `~/.wechatbot/credentials.json` (the SDK default).
Override globally with the `WDNDDS_WECHAT_CRED_PATH` environment variable
or per-instance with `Wechat(cred_path=...)`. Pass `force=True` to
`start_login()` to discard the stored credentials and re-issue a QR.

State machine
-------------

`idle → awaiting_scan → scanned → confirmed → running` (long-poll alive),
with `expired` and `error` as terminal failure branches. `cancel_login()`
just hides the QR — the underlying SDK coroutine runs to natural completion
since `wechatbot.WeChatBot` doesn't expose a public login cancel.

Error classes
-------------

`Wechat.NoContextError` / `Wechat.AuthError` / `Wechat.MediaError` /
`Wechat.WeChatBotError` are re-exported as nested attributes so callers
can `except wdndds.Wechat.NoContextError` without taking a direct dep on
`wechatbot`. The originals stay importable too.

## License

MIT. See `LICENSE`.