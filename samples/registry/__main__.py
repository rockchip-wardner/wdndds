"""Dispatch ``python -m samples.registry {register|discover}``."""

from __future__ import annotations

import sys

from . import discover, register

_COMMANDS = {"register": register, "discover": discover}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        print(
            f"usage: python -m samples.registry {{ {'|'.join(sorted(_COMMANDS))} }} [...]",
            file=sys.stderr,
        )
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    sys.argv = [cmd, *rest]
    return _COMMANDS[cmd].main()


if __name__ == "__main__":
    sys.exit(main())
