"""Работа с файлами OneDrive «только в облаке».

Чтение такого файла скачивает его автоматически. После обработки программа
возвращает файл в состояние «только в облаке» (attrib +U -P), чтобы не забивать
диск. На других системах всё это ничего не делает.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .media import NO_WINDOW

# FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE
_CLOUD_ONLY = 0x400000 | 0x40000 | 0x1000


def is_cloud_only(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(os.stat(path).st_file_attributes & _CLOUD_ONLY)
    except OSError:
        return False


def free_up(path: Path) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(["attrib", "+U", "-P", str(path)], capture_output=True, **NO_WINDOW)


class Hydration:
    """Следит, чтобы скачанные для обработки файлы не съели весь диск."""

    def __init__(self, enabled: bool, disk: Path, reserve_bytes: int, log=print):
        self.enabled = enabled and sys.platform == "win32"
        self.disk = disk
        self.reserve = reserve_bytes
        self.log = log
        self.downloaded: dict[int, Path] = {}  # id файла → где он сейчас

    def before_read(self, path: Path, size: int) -> bool:
        """Вызывать перед чтением. Возвращает True, если файл придётся скачать."""
        if not self.enabled or not is_cloud_only(path):
            return False
        if shutil.disk_usage(self.disk).free - size < self.reserve:
            self.release()
        return True

    def track(self, file_id: int, path: Path) -> None:
        if self.enabled:
            self.downloaded[file_id] = path

    def moved(self, file_id: int, new_path: Path) -> None:
        if file_id in self.downloaded:
            self.downloaded[file_id] = new_path

    def release(self) -> None:
        if not self.downloaded:
            return
        self.log(f"Освобождаю место: {len(self.downloaded)} файлов снова только в облаке")
        for path in self.downloaded.values():
            if path.exists():
                free_up(path)
        self.downloaded.clear()
