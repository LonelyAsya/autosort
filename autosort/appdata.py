"""Где лежат настройки, база и списки тегов, и правка путей в config.toml."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path


def data_dir() -> Path:
    """Папка данных программы. Если config.toml лежит рядом с exe — работаем «переносной» версией."""
    if getattr(sys, "frozen", False):
        portable = Path(sys.executable).parent
        if (portable / "config.toml").exists():
            return portable
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Autosort"


def ensure_config(folder: Path) -> Path:
    """Создаёт config.toml и пример списка тегов при первом запуске."""
    config = folder / "config.toml"
    if config.exists():
        return config
    data = resources.files("autosort").joinpath("data")
    (folder / "rules").mkdir(parents=True, exist_ok=True)
    config.write_bytes(data.joinpath("config.example.toml").read_bytes())
    for item in data.joinpath("rules").iterdir():
        target = folder / "rules" / item.name
        if not target.exists():
            target.write_bytes(item.read_bytes())
    return config


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def set_setting(config: Path, section: str, key: str, value: str) -> None:
    """Меняет одну строку «key = value» в секции, не трогая остальной файл и комментарии.

    value — уже готовое значение TOML (строка в кавычках, true/false, число).
    """
    text = config.read_text(encoding="utf-8")
    header = f"[{section}]"
    found = re.search(rf"^{re.escape(header)}[ \t]*\n(?:(?!^\[).*\n?)*", text, re.MULTILINE)
    line = f"{key} = {value}"
    if found is None:
        text = text.rstrip("\n") + f"\n\n{header}\n{line}\n"
    else:
        body = found.group(0)
        pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
        if pattern.search(body):
            new_body = pattern.sub(lambda _: line, body, count=1)
        else:
            new_body = body.replace(header + "\n", f"{header}\n{line}\n", 1)
        text = text[:found.start()] + new_body + text[found.end():]
    config.write_text(text, encoding="utf-8")


def toml_value(value) -> str:
    """Значение Python → строка TOML для set_setting."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


_CATEGORY_BLOCK = re.compile(r"^\[\[categories\.list\]\][ \t]*\n(?:(?!^\[).*\n?)*", re.MULTILINE)


def save_categories(config: Path, default: str, categories: list[tuple[str, str]]) -> None:
    """Переписывает список категорий (имя, файл тегов) в config.toml по порядку приоритета.

    Остальной файл и комментарии не трогаются."""
    text = config.read_text(encoding="utf-8")
    blocks = "".join(f"[[categories.list]]\nname = {toml_value(n)}\ntags_file = {toml_value(f)}\n\n"
                     for n, f in categories)
    first = _CATEGORY_BLOCK.search(text)
    if first:
        start = first.start()
        text = _CATEGORY_BLOCK.sub("", text)
        text = text[:start] + blocks + text[start:]
    else:
        section = re.search(r"^\[categories\][ \t]*\n(?:(?!^\[).*\n?)*", text, re.MULTILINE)
        if section:
            text = text[:section.end()] + ("\n" if not text[:section.end()].endswith("\n\n") else "") \
                + blocks + text[section.end():]
        else:
            text = text.rstrip("\n") + "\n\n[categories]\n\n" + blocks
    config.write_text(text, encoding="utf-8")
    set_setting(config, "categories", "default", toml_value(default))


_CUSTOM_BLOCK = re.compile(r"^\[\[custom_folders\]\][ \t]*\n(?:(?!^\[).*\n?)*", re.MULTILINE)


def save_custom_folders(config: Path, folders: list[dict]) -> None:
    """Переписывает свои папки верхнего уровня: [{name, tags_file, patterns}] по порядку проверки."""
    text = config.read_text(encoding="utf-8")
    blocks = "".join(
        f"[[custom_folders]]\nname = {toml_value(f['name'])}\ntags_file = {toml_value(f['tags_file'])}\n"
        f"patterns = {toml_value(list(f.get('patterns', [])))}\n\n" for f in folders)
    first = _CUSTOM_BLOCK.search(text)
    if first:
        start = first.start()
        text = _CUSTOM_BLOCK.sub("", text)
        text = text[:start] + blocks + text[start:]
    elif blocks:
        text = (text.rstrip("\n") + "\n\n# Свои папки рядом с «Камерой», «Артами» и т. д.: файл попадает в первую,\n"
                "# у которой совпала маска имени (patterns) или хоть один тег из файла tags_file.\n" + blocks)
    config.write_text(text.rstrip("\n") + "\n", encoding="utf-8")


def safe_file_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned or "category"


def set_path_setting(config: Path, key: str, value: str) -> None:
    set_setting(config, "paths", key, _toml_string(value))


def open_path(path: Path) -> None:
    """Открывает файл или папку программой по умолчанию."""
    if os.name == "nt":
        if path.suffix.lower() in (".toml", ".txt") and path.is_file():
            subprocess.Popen(["notepad.exe", str(path)])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)])
