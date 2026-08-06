from __future__ import annotations


class DDSError(RuntimeError):
    """Raised when an underlying FastDDS operation fails."""


class ReturnCodeError(DDSError):
    """Raised when a FastDDS call returns a non-OK return code."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(f"FastDDS return code {code}: {message}")


def check(code: int, op: str, ok: int) -> None:
    # Fast-DDS-Python 1.x 的 ``DataWriter::write`` 返回 ``bool``（True = 成功），
    # 2.x 之后切回 ``ReturnCode_t``（RETCODE_OK = 0）；同一种"成功"在两个版本
    # 下编码不同，这里统一兜底：True / False / RETCODE_OK 都算成功，其它一律
    # 当作失败抛 ReturnCodeError。
    if code is True or code == ok:
        return
    raise ReturnCodeError(code, f"{op} failed")
