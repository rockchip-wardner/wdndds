# samples/registry

A two-process sample that demonstrates a **registration & discovery** pattern
on top of `pyiot.dds`.

- `register.py` — publishes this node's id, name, location, and capabilities
  to the `pyiot/registry` topic at a fixed interval.
- `discover.py` — subscribes to the same topic with `transient_local` QoS so
  every currently alive registrar's *last* published value is delivered
  automatically. New writes (from re-announces) overwrite the local record.
- `common.py` — shared topic name + JSON payload helpers.

## Payload

```json
{
  "node_id": "bot-01",
  "node_name": "warehouse-bot",
  "location": {"zone": "A", "x": 12.5, "y": 3.0, "z": 0.0},
  "capabilities": ["navigate", "pick", "place"],
  "timestamp": "2026-07-27T03:11:42+00:00",
  "ttl": 30
}
```

The topic uses eProsima's built-in `StringTopicDataType` (a `std::string`
on the wire), registered through the new
[`pyiot.dds.Participant.register_string_type`](../README.md) helper so the
sample never touches raw `fastdds`.

## Run

```bash
# terminal 1
python -m samples.registry register \
    --id bot-01 --name "warehouse-bot" --zone A \
    --x 12.5 --y 3.0 \
    --cap navigate --cap pick --cap place

# terminal 2
python -m samples.registry discover

# terminal 3
python -m samples.registry register \
    --id bot-02 --name "loader" --zone B \
    --x 0 --y 0 \
    --cap navigate --cap report_status
```

`discover` immediately prints both `bot-01` and `bot-02` (thanks to
`transient_local`). It updates as registrars re-announce and exits cleanly
on `Ctrl+C`.

## Options

`register`:

| flag | default | meaning |
| --- | --- | --- |
| `--id` | random 8 hex | stable node id |
| `--name` | `node` | human readable label |
| `--zone` | `default` | zone label |
| `--x`, `--y`, `--z` | `0.0` | position in metres |
| `--cap` | `navigate`, `report_status` | capability tag, repeatable |
| `--domain` | `0` | DDS domain id |
| `--interval` | `5.0` | seconds between publishes |

`discover`:

| flag | default | meaning |
| --- | --- | --- |
| `--domain` | `0` | DDS domain id |
| `--interval` | `1.0` | seconds between polls |
| `--once` | off | print once and exit |

## Prerequisites

Same as `pyiot` itself — the eProsima Fast DDS Python bindings must be
importable. See the [top-level README](../README.md#prerequisites).