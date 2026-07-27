"""End-to-end capability sample: one process = one device.

Each script instance IS one device. It:

1. Registers its own capabilities (with callbacks) under ``--name``.
2. Subscribes to discovery. When a peer appears, invokes one capability on
   it (``CameraQuery`` on the ``大门正门`` instance) and prints the reply.
3. Loops until Ctrl-C.

Run multiple instances on the same host (or same DDS domain) with different
``--name`` values to exercise cross-device discovery:

    Terminal A:  python samples/register_and_find.py --name Gate1Office
    Terminal B:  python samples/register_and_find.py --name Gate2Office

Both A and B will register, see each other appear, and trade CameraQuery
calls.
"""

import argparse
import time

import wdiot
from wdiot import AudioQuery, CameraInput, CameraQuery, MicInput, SpeakerOutput


def make_callbacks(label: str) -> dict:
    def on_frame(payload, reply):
        print(f"[{label}] CameraInput <- {payload!r}", flush=True)
        reply({"ok": True})

    def on_query(payload, reply):
        print(f"[{label}] CameraQuery <- {payload!r}", flush=True)
        reply({"answer": f"served by {label}", "q": payload.get("q")})

    def on_play(payload, reply):
        print(f"[{label}] SpeakerOutput <- {payload!r}", flush=True)
        reply({"played": True, "by": label})

    def on_mic(payload, reply):
        print(f"[{label}] MicInput <- {payload!r}", flush=True)
        reply({"ok": True})

    def on_audio_query(payload, reply):
        print(f"[{label}] AudioQuery <- {payload!r}", flush=True)
        reply({"decibel": -30, "by": label})

    return {
        "frame": on_frame,
        "query": on_query,
        "play": on_play,
        "mic": on_mic,
        "audio_q": on_audio_query,
    }


def build_capabilities(cb: dict) -> list:
    return [
        CameraInput("大门正门",       callback=cb["frame"]),
        CameraInput("电动车路口",     callback=cb["frame"]),
        CameraInput("人行入口",       callback=cb["frame"]),
        CameraQuery("大门正门",       callback=cb["query"]),
        CameraQuery("电动车路口",     callback=cb["query"]),
        SpeakerOutput("大门喇叭",     callback=cb["play"]),
        SpeakerOutput("门禁提示喇叭", callback=cb["play"]),
        MicInput("大门拾音器",       callback=cb["mic"]),
        AudioQuery("大门拾音器",     callback=cb["audio_q"]),
    ]


def main(my_name) -> None:
    cb = make_callbacks(my_name)

    devices: dict[str, wdiot.Device] = {}

    def call_camquery(peer: wdiot.Device) -> None:
        try:
            fut = peer.CameraQuery["大门正门"]({"q": "what do you see?"})
            print(
                f"[{my_name}] -> {peer.name}.CameraQuery reply: "
                f"{fut.result(timeout=2.0)}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[{my_name}] -> {peer.name}.CameraQuery error: {exc!r}",
                flush=True,
            )

    def on_event(device: wdiot.Device, event: wdiot.Event) -> None:
        if device.name == my_name:
            return
        if event.kind == "added":
            devices[device.name] = device
            cap_names = sorted(c.__name__ for c in device.capabilities)
            print(f"[{my_name}] + {device.name} caps={cap_names}", flush=True)
            call_camquery(device)
        elif event.kind == "removed":
            devices.pop(device.name, None)
            print(f"[{my_name}] - {device.name}", flush=True)

    with wdiot.connect():
        wdiot.register(my_name, build_capabilities(cb))
        wdiot.start_scan(on_event)
        print(
            f"[{my_name}] registered; scanning for peers. ctrl-c to exit.",
            flush=True,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            wdiot.stop_scan(on_event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="device name to register")
    args = parser.parse_args()
    main(args.name)
