#!/usr/bin/env python3
"""Register this node's presence, location, and capabilities.

Publishes a JSON registration to the ``wdiot/registry`` topic on a fixed
interval. The payload carries:

- ``node_id``     — stable unique id for this node
- ``node_name``   — human readable label
- ``location``    — {zone, x, y, z} describing the current position
- ``capabilities``— list of string tags naming what this node can do
- ``timestamp``   — UTC ISO 8601 of the publish
- ``ttl``         — seconds the registration is considered fresh

A discoverer joining later with ``transient_local`` QoS will receive the
last value from each currently alive writer automatically.

Run it from the repo root so the ``samples`` package is importable::

    python -m samples.registry.register --id bot-01 --zone A --x 12.5 --y 3.0 \\
        --cap navigate --cap pick --cap place
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import uuid

from wdiot import dds

from .common import TOPIC_NAME, encode, make_registration


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--id", dest="node_id", default=uuid.uuid4().hex[:8], help="unique node id")
    p.add_argument("--name", default="node", help="human readable node name")
    p.add_argument("--zone", default="default", help="zone label (e.g. 'A', 'B-12')")
    p.add_argument("--x", type=float, default=0.0)
    p.add_argument("--y", type=float, default=0.0)
    p.add_argument("--z", type=float, default=0.0)
    p.add_argument(
        "--cap",
        action="append",
        default=["navigate", "report_status"],
        help="capability tag (repeatable)",
    )
    p.add_argument("--domain", type=int, default=0)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between publishes")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    location = {"zone": args.zone, "x": args.x, "y": args.y, "z": args.z}

    stop = {"flag": False}

    def _on_signal(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    with dds.Participant(domain_id=args.domain, name=args.name) as participant:
        type_name = participant.register_string_type("registry")
        topic = participant.create_topic(TOPIC_NAME, type_name)

        writer_qos = dds.transient_local_writer_qos()
        with (
            participant.create_publisher() as pub,
            pub.create_writer(topic, qos=writer_qos) as writer,
        ):
            print(
                f"[register] node_id={args.node_id} topic={TOPIC_NAME} "
                f"interval={args.interval}s domain={args.domain}",
                file=sys.stderr,
            )
            while not stop["flag"]:
                reg = make_registration(
                    node_id=args.node_id,
                    node_name=args.name,
                    location=location,
                    capabilities=args.cap,
                )
                try:
                    writer.write(encode(reg))
                except dds.ReturnCodeError as exc:
                    print(f"[register] write failed: {exc}", file=sys.stderr)
                else:
                    print(
                        f"[register] {reg['timestamp']} caps={reg['capabilities']} loc={location}",
                        file=sys.stderr,
                    )
                for _ in range(int(args.interval * 10)):
                    if stop["flag"]:
                        break
                    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
