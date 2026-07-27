#!/usr/bin/env python3
"""Discover all currently registered nodes.

Subscribes to the ``pyiot/registry`` topic with ``transient_local`` QoS so
every live writer's last published value is delivered on join. New writes
arrive as nodes re-announce. A local dict keyed by ``node_id`` keeps the
freshest record per node.

Run it from the repo root::

    python -m samples.registry.discover --domain 0
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from pyiot import dds

from .common import TOPIC_NAME, decode


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--domain", type=int, default=0)
    p.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    p.add_argument("--once", action="store_true", help="print once and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    known: dict[str, dict] = {}

    stop = {"flag": False}

    def _on_signal(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    with dds.Participant(domain_id=args.domain, name="discoverer") as participant:
        type_name = participant.register_string_type("registry")
        topic = participant.create_topic(TOPIC_NAME, type_name)

        reader_qos = dds.transient_local_reader_qos()
        with (
            participant.create_subscriber() as sub,
            sub.create_reader(topic, qos=reader_qos) as reader,
        ):
            print(f"[discover] watching {TOPIC_NAME} on domain {args.domain}", file=sys.stderr)
            while not stop["flag"]:
                samples = reader.take(100)
                for data, info in samples:
                    if not info.valid_data:
                        continue
                    try:
                        reg = decode(data)
                    except (ValueError, TypeError) as exc:
                        print(f"[discover] bad payload: {exc}", file=sys.stderr)
                        continue
                    known[reg["node_id"]] = reg
                if samples:
                    print(f"[discover] {len(known)} known node(s):", file=sys.stderr)
                    for nid, reg in sorted(known.items()):
                        loc = reg["location"]
                        print(
                            f"  - {nid} name={reg['node_name']!r} "
                            f"zone={loc.get('zone')} "
                            f"pos=({loc.get('x')},{loc.get('y')},{loc.get('z')}) "
                            f"caps={reg['capabilities']} "
                            f"seen={reg['timestamp']}",
                            file=sys.stderr,
                        )
                if args.once:
                    break
                for _ in range(int(args.interval * 10)):
                    if stop["flag"]:
                        break
                    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
