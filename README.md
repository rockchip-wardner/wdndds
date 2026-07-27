# pyiot

Pythonic wrapper around [eProsima Fast DDS](https://www.eprosima.com/en/products/all-products/fast-dds)
Python bindings. The package re-exports the bindings through a small, ergonomic
facade — context managers, QoS presets, lazy import — so you can spin up a DDS
participant, publisher, and subscriber in a few lines.

`pyiot` is the distribution / import name; the FastDDS surface lives under
`pyiot.dds`.

## Prerequisites

`pyiot` has **no PyPI dependencies** — it is a pure-Python facade. The native
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

## Install `pyiot`

```bash
pip install pyiot
```

## Diagnose at runtime

```python
from pyiot import dds
print(dds.check_runtime())
# -> {'available': True, 'module': 'fastdds', 'path': '...', 'version': '2.10.x'}
```

If `available` is `False`, the returned dict carries an `error` field with
install instructions.

## Quick start

```python
from pyiot import dds

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
from pyiot import dds

writer_qos = dds.reliable_writer_qos()
reader_qos = dds.best_effort_reader_qos()
```

## Build from source

```bash
python -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple build pytest ruff
.venv/bin/python -m build
```

This produces `dist/pyiot-<version>-py3-none-any.whl` and the matching sdist.

## Project layout

```
src/pyiot/
├── __init__.py
├── _version.py
├── py.typed
└── dds/
    ├── __init__.py     # re-exports the public surface
    ├── _fastdds.py     # lazy loader for the native `fastdds` module
    ├── exceptions.py
    ├── qos.py          # QoS factories and presets
    ├── domain.py       # Participant
    ├── topic.py        # Topic
    ├── publisher.py    # Publisher, Writer
    └── subscriber.py   # Subscriber, Reader
```

## License

MIT. See `LICENSE`.