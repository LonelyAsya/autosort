"""Достаёт из CHANGELOG.md раздел нужной версии: python packaging/release_notes.py 1.3.2 > notes.md"""
import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("v")
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.S | re.M)
if not match:
    sys.exit(f"В CHANGELOG.md нет раздела [{version}]")
sys.stdout.reconfigure(encoding="utf-8")
print(match.group(1).strip())
print("\nВсе изменения: [CHANGELOG.md](https://github.com/LonelyAsya/autosort/blob/main/CHANGELOG.md)")
