# wdiot - wardner.iot

Pythonic wrapper around [eProsima Fast DDS](https://www.eprosima.com/en/products/all-products/fast-dds)
Python bindings. The package re-exports the bindings through a small, ergonomic
facade — context managers, QoS presets, lazy import — so you can spin up a DDS
participant, publisher, and subscriber in a few lines.

`wdiot` is the distribution / import name; the FastDDS surface lives under
`wdiot.dds`. On top of that, `wdiot` itself exposes a small **capability
registry** so devices on the same DDS domain can advertise what they do and
call each other with one-liners.

## Prerequisites

`wdiot` has **no PyPI dependencies** — it is a pure-Python facade. The native
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

## Install `wdiot`

```bash
pip install wdiot
```

## Diagnose at runtime

```python
from wdiot import dds
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
import wdiot
from wdiot import CameraInput, CameraQuery, SpeakerOutput

with wdiot.connect():
    wdiot.register("Gate1Office", [
        CameraInput("大门正门",   callback=on_frame),
        CameraQuery("大门正门",   callback=on_query),
        SpeakerOutput("大门喇叭", callback=on_play),
    ])
```

Discovery is push-based: register a callback and the user-owned dictionary is
kept in sync.

```python
import wdiot

devices: dict[str, wdiot.Device] = {}

def on_event(device: wdiot.Device, event: wdiot.Event) -> None:
    if event.kind == "added":
        devices[device.name] = device
        print(f"+ {device.name} caps={[c.__name__ for c in device.capabilities]}")
    elif event.kind == "removed":
        devices.pop(device.name, None)

with wdiot.connect():
    wdiot.start_scan(on_event)
    # ... when a Gate1Office appears in `devices`:
    gate1 = devices["Gate1Office"]
    fut = gate1.CameraQuery["大门正门"]({"q": "what do you see?"})
    print(fut.result(timeout=2.0))
```

`removed` events are best-effort: they rely on FastDDS built-in participant
liveliness. If the listener API isn't available on your binding, only `added`
events fire and you manage your dictionary yourself.

### Predefined capabilities

| Class | DDS type | Default (no callback) |
|---|---|---|
| `CameraInput` | `video_input` | drops frames |
| `CameraQuery` | `video_query` | `{"answer": "unconfigured"}` |
| `CameraDescribe` | `video_describe` | `{"description": "unconfigured"}` |
| `MicInput` | `audio_input` | drops samples |
| `SpeakerOutput` | `audio_output` | `{"played": False, ...}` |
| `AudioQuery` | `audio_query` | `{"answer": "unconfigured"}` |
| `AudioAnalyse` | `audio_analyse` | `{"result": None}` |
| `HDMIDisplay` | `hdmi_display` | `{"displayed": False, ...}` |

Subclass `Capability` to add your own — set `capability_type` and override
`_default_handle`.

### Callback contract

```python
def my_handler(payload, reply):
    # payload is whatever the caller passed to cap(...)
    # call reply(value) exactly once with the result you want the caller to see
    reply({"echo": payload})
```

For remote invocations the same callback runs on a wdiot background thread
when a message arrives on the capability topic; for local invocations
(`cap(...)` on a freshly constructed instance) it runs synchronously and
`cap(...)` returns the reply value.

### End-to-end sample

Each script instance IS one device: it registers its own capabilities and
also scans for other devices. Run two instances with different `--name`
values on the same host (or any host on the same DDS domain) and they will
discover each other and trade `CameraQuery` calls:

```bash
# Terminal A
python samples/register_and_find.py --name Gate1Office

# Terminal B
python samples/register_and_find.py --name Gate2Office
```

## Low-level DDS facade (`wdiot.dds`)

```python
from wdiot import dds

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
from wdiot import dds

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

This produces `dist/wdiot-<version>-py3-none-any.whl` and the matching sdist.

## Project layout

```
src/wdiot/
├── __init__.py        # public API: Capability classes, connect, register,
│                      #             start_scan, stop_scan, Device, Event
├── _version.py
├── py.typed
├── capabilities.py    # Capability base + 8 predefined subclasses
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

## License

MIT. See `LICENSE`.