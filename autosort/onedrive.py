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


# Ошибки Windows, при которых OneDrive не отдаст ни один файл, пока его не починить.
# Python показывает их как «[Errno 22] Invalid argument», настоящий код теряется.
_PROVIDER_DOWN = {
    362,  # ERROR_CLOUD_FILE_PROVIDER_NOT_RUNNING — «Поставщик облачного файла не работает»
    404,  # ERROR_CLOUD_FILE_PROVIDER_TERMINATED
}


class CloudUnavailable(Exception):
    """OneDrive не отдаёт файлы — дальше обрабатывать бессмысленно."""


def windows_read_error(path: Path) -> tuple[int, str] | None:
    """Пробует прочитать первый байт средствами Windows и возвращает (код, текст) ошибки.

    Нужно, потому что open() в Python сводит облачные ошибки к Errno 22."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0, None)  # GENERIC_READ, OPEN_EXISTING
    if handle in (None, wintypes.HANDLE(-1).value):
        code = ctypes.get_last_error()
    else:
        buf = ctypes.create_string_buffer(1)
        read = wintypes.DWORD()
        ok = k32.ReadFile(handle, buf, 1, ctypes.byref(read), None)
        code = 0 if ok else ctypes.get_last_error()
        k32.CloseHandle(handle)
    if not code:
        return None
    return code, ctypes.FormatError(code).strip()


def explain_read_error(path: Path, error: Exception) -> str:
    """Текст ошибки с настоящей причиной от Windows; бросает CloudUnavailable, если OneDrive не работает."""
    if not isinstance(error, OSError) or getattr(error, "winerror", None):
        return str(error)
    try:
        real = windows_read_error(path)
    except Exception:  # диагностика не должна сама ронять разбор
        real = None
    if real is None:
        return str(error)
    code, text = real
    if code in _PROVIDER_DOWN:
        raise CloudUnavailable(
            f"OneDrive не отдаёт файлы: «{text}» (код {code}). Обработка остановлена, файлы не тронуты.\n"
            "Что сделать: щёлкните значок OneDrive в трее — если синхронизация на паузе, возобновите её. "
            "Если не поможет, перезапустите OneDrive (значок → шестерёнка → «Закрыть OneDrive», "
            "затем откройте OneDrive из меню «Пуск»). Проверьте, что файл открывается в проводнике, "
            "и запустите разбор снова — уже разобранное пропустится.")
    return f"{text} (код Windows {code})"


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
