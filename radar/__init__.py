"""M22 Product Radar — система рыночной разведки для M22 / RadioSync."""
from pathlib import Path

_VF = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """Версия читается из файла при каждом обращении — без перезапуска сервера."""
    try:
        return _VF.read_text(encoding="utf-8").strip() or "0.1.0"
    except OSError:
        return "0.1.0"


__version__ = get_version()
