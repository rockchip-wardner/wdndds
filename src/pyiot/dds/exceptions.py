from __future__ import annotations


class DDSError(RuntimeError):
    """Raised when an underlying FastDDS operation fails."""


class ReturnCodeError(DDSError):
    """Raised when a FastDDS call returns a non-OK return code."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(f"FastDDS return code {code}: {message}")


def check(code: int, op: str, ok: int) -> None:
    if code != ok:
        raise ReturnCodeError(code, f"{op} failed")
