"""Обновление из самой программы: проверка последнего релиза на GitHub, скачивание и запуск установщика.

На GitHub уходит только запрос «какой последний релиз» и скачивание установщика — без данных пользователя.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

REPO = "LonelyAsya/autosort"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"Autosort/{__version__}"


@dataclass
class Release:
    version: str
    notes: str
    page: str
    setup_url: str | None
    setup_name: str | None
    setup_size: int | None
    setup_sha256: str | None


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and (cur is None or new > cur)


def parse_release(data: dict) -> Release:
    setup = next((a for a in data.get("assets", [])
                  if a.get("name", "").startswith("Autosort-Setup-") and a["name"].endswith(".exe")), None)
    digest = (setup or {}).get("digest") or ""
    return Release(
        version=data.get("tag_name", "").lstrip("v"),
        notes=(data.get("body") or "").strip(),
        page=data.get("html_url") or RELEASES_PAGE,
        setup_url=setup and setup.get("browser_download_url"),
        setup_name=setup and setup.get("name"),
        setup_size=setup and setup.get("size"),
        setup_sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
    )


def latest_release(timeout: float = 15) -> Release:
    request = urllib.request.Request(API_LATEST, headers={
        "User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_release(json.load(response))


def short_notes(notes: str, limit: int = 1200) -> str:
    """Патчноут для окна: без markdown-разметки и ссылки на весь CHANGELOG."""
    lines = []
    for line in notes.splitlines():
        if line.startswith("Все изменения:"):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\*\*(.+?)\*\*|`(.+?)`|_(.+?)_", lambda m: next(g for g in m.groups() if g), line)
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def installed_dir() -> Path | None:
    """Папка установленной программы (есть деинсталлятор рядом с exe); None — portable или запуск из исходников."""
    if not getattr(sys, "frozen", False):
        return None
    folder = Path(sys.executable).parent
    return folder if (folder / "unins000.exe").exists() else None


def download(release: Release, progress: Callable[[int, int], None] | None = None,
             should_stop: Callable[[], bool] | None = None) -> Path:
    if not release.setup_url:
        raise RuntimeError("В релизе нет установщика")
    folder = Path(tempfile.gettempdir()) / "Autosort-update"
    folder.mkdir(exist_ok=True)
    target = folder / release.setup_name
    partial = target.with_suffix(".part")
    sha = hashlib.sha256()
    request = urllib.request.Request(release.setup_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or release.setup_size or 0)
        done = 0
        while chunk := response.read(1024 * 256):
            if should_stop and should_stop():
                out.close()
                partial.unlink(missing_ok=True)
                raise InterruptedError
            out.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if release.setup_size and done != release.setup_size:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Установщик скачался не полностью: {done} из {release.setup_size} байт")
    if release.setup_sha256 and sha.hexdigest() != release.setup_sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError("Контрольная сумма установщика не совпала — файл повреждён, попробуйте ещё раз")
    partial.replace(target)
    return target


def _writable(folder: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def run_installer(setup: Path, folder: Path) -> None:
    """Тихая установка поверх текущей версии; установщик сам закроет программу и запустит новую."""
    args = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1",
            # В Program Files без прав администратора не записать — тогда установщик попросит их.
            "/CURRENTUSER" if _writable(folder) else "/ALLUSERS"]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def run_uninstaller(folder: Path) -> None:
    # /SILENT — без повторного «Вы уверены?» (уже спросили в программе); вопрос про данные остаётся.
    flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    subprocess.Popen([str(folder / "unins000.exe"), "/SILENT"], creationflags=flags, close_fds=True)
