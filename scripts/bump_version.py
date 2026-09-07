"""Повышение версии радара. Правило: каждый запрос заказчика = +1 к третьему числу; после 99 — +1 ко второму и третье = 0;
первое число меняется только вручную (python scripts/bump_version.py major)."""
import sys
from pathlib import Path

VF = Path(__file__).resolve().parent.parent / "VERSION"
major, minor, patch = (int(x) for x in VF.read_text(encoding="utf-8").strip().split("."))
mode = sys.argv[1] if len(sys.argv) > 1 else "patch"
if mode == "major":
    major, minor, patch = major + 1, 0, 0
elif mode == "set":
    major, minor, patch = (int(x) for x in sys.argv[2].split("."))
else:
    patch += 1
    if patch > 99:
        minor, patch = minor + 1, 0
VF.write_text(f"{major}.{minor}.{patch}
", encoding="utf-8")
print(f"{major}.{minor}.{patch}")
