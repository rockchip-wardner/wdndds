from __future__ import annotations

from ._cshim import check_runtime
from .exceptions import DDSError

__all__ = [
    "DDSError",
    "check_runtime",
]