"""M22 Product Radar — система рыночной разведки для M22 / RadioSync."""
from pathlib import Path

_VF = Path(__file__).resolve().parent.parent / "VERSION"
try:
    __version__ = _VF.read_text(encoding="utf-8").strip() or "0.1.0"
except OSError:
    __version__ = "0.1.0"
