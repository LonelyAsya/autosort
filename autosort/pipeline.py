"""Проход по месяцам: разбор, план переноса, перенос с журналом и откат."""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from tqdm import tqdm

from .analyze import Analyzer, Tagger, hash_media, match_categories
from .config import Config
from .db import Database
from .media import is_media
from .onedrive import Hydration


class Stopped(Exception):
    """Пользователь нажал «Остановить». Всё сделанное до этого сохранено."""


@dataclass
class PlannedMove:
    file_id: int
    src: Path
    dst: Path
    type: str
    category: str | None


class LazyTagger:
    """Модель грузится только когда впервые нужна, и только один раз."""

    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self._tagger = None
        self._failed = False

    def __call__(self) -> Tagger | None:
        if not self.cfg.tagger_enabled or self._failed:
            return None
        if self._tagger is None:
            try:
                from .tagger import WD14Tagger

                self.log(f"Загружаю тегер {self.cfg.tagger_model} (в первый раз скачивается ~450 МБ)…")
                self._tagger = WD14Tagger(self.cfg.tagger_model, self.cfg.tagger_device,
                                          self.cfg.general_threshold, self.cfg.character_threshold)
                self.log(f"Тегер работает на: {self._tagger.provider}")
            except Exception as e:
                self.log(f"Тегер недоступен ({e}). Файлы без явных признаков уйдут на ручной разбор.")
                self._failed = True
                return None
        return self._tagger


def walk_media(root: Path, exclude: list[Path]) -> Iterator[Path]:
    exclude = [e.resolve() for e in exclude]
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath).resolve()
        dirnames[:] = sorted(n for n in dirnames if (d / n) not in exclude and not n.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if is_media(p):
                yield p


def media_folders(cfg: Config) -> list[tuple[str, Path]]:
    """Все папки источника, где прямо внутри лежат файлы, с путём от источника.

    Работает при любой вложенности: «Альбом камеры/2024/05/фото» даёт («2024/05», папка).
    Файлы прямо в корне источника получают пустое имя.
    """
    src = cfg.source
    skip = [cfg.target, cfg.cache_dir]
    folders: dict[Path, None] = {}
    for f in walk_media(src, skip):
        folders.setdefault(f.parent, None)
    return [(d.relative_to(src).as_posix() if d != src else "", d) for d in folders]


def folder_choices(folders: list[tuple[str, Path]]) -> list[str]:
    """Варианты для выбора: и годы целиком, и отдельные месяцы."""
    choices: set[str] = set()
    for name, _ in folders:
        parts = name.split("/") if name else []
        for i in range(1, len(parts) + 1):
            choices.add("/".join(parts[:i]))
    return sorted(choices)


class Pipeline:
    def __init__(self, cfg: Config, db: Database, get_tagger: Callable[[], Tagger | None] | None = None,
                 log=print, progress: bool | Callable[[str, int, int], None] = True,
                 should_stop: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.db = db
        self.log = log
        # True — полоска в консоли, функция (подпись, сделано, всего) — для окна программы
        self.progress = progress
        self.should_stop = should_stop
        # Паузы перед повторами: OneDrive иногда не отдаёт файл с первого раза
        # (Errno 22, недокачанный файл), особенно пока освобождает место.
        self.retry_delays: tuple[float, ...] = (5, 30)
        self.errors = 0
        sauce = None
        if cfg.saucenao_enabled and cfg.saucenao_api_key:
            from .authors import SauceNao

            sauce = SauceNao(cfg.saucenao_api_key, cfg.saucenao_min_similarity, cfg.saucenao_delay)
        self.analyzer = Analyzer(cfg, get_tagger or LazyTagger(cfg, log), sauce)

    def _iter(self, items: list, label: str) -> Iterator:
        report = self.progress if callable(self.progress) else None
        bar = None if report else tqdm(total=len(items), desc=label, unit="файл", disable=not self.progress)
        for i, item in enumerate(items):
            if self.should_stop and self.should_stop():
                raise Stopped
            if report:
                report(label, i, len(items))
            yield item
            if bar:
                bar.update()
        if report:
            report(label, len(items), len(items))
        if bar:
            bar.close()

    def _hydration(self) -> Hydration:
        return Hydration(self.cfg.onedrive_free_space, self.cfg.source, self.cfg.onedrive_reserve_bytes, self.log)

    def months(self, only: str | None = None) -> list[tuple[str, Path]]:
        """Папки для обработки по одной. only — «2024» (весь год) или «2024/05»."""
        result = media_folders(self.cfg)
        if only:
            only = only.strip("/")
            result = [m for m in result if m[0] == only or m[0].startswith(only + "/")]
        return result

    def _files_of(self, name: str, folder: Path) -> list[Path]:
        return sorted(f for f in folder.iterdir() if f.is_file() and is_media(f))

    # --- разбор ---

    def scan(self, month: str | None = None, full: bool = False) -> list[PlannedMove]:
        """Разбирает новые файлы источника и возвращает план переноса. Файлы не трогает."""
        plan: list[PlannedMove] = []
        reserved: set[Path] = set()
        for name, folder in self.months(month):
            files = self._files_of(name, folder)
            hydration = self._hydration()
            try:
                for path in self._iter(files, name or "(корень)"):
                    file_id = self._scan_file(path, name, full, hydration)
                    if file_id is None:
                        continue
                    row = self.db.get(file_id)
                    dst = unique_path(self.target_for(row, path.name), reserved)
                    reserved.add(dst)
                    plan.append(PlannedMove(file_id, path, dst, row["type"], row["category"]))
            finally:
                hydration.release()
        if self.errors:
            self.log(f"Не удалось разобрать файлов: {self.errors}. Они останутся на месте; "
                     "при следующем запуске программа попробует их снова.")
        return plan

    def _scan_file(self, path: Path, month: str, full: bool, hydration: Hydration) -> int | None:
        st = path.stat()
        row = self.db.get_by_path(path)
        if (row is not None and not full and row["status"] == "analyzed"
                and row["size"] == st.st_size and row["mtime"] == st.st_mtime):
            return row["id"]
        downloaded = hydration.before_read(path, st.st_size)
        try:
            a = self._with_retries(path, lambda: self.analyzer.analyze(path))
        except Stopped:
            raise
        except Exception as e:
            self.errors += 1
            self.log(f"Ошибка: {path.name}: {e}")
            file_id = self.db.save_file(path, month=month, size=st.st_size, mtime=st.st_mtime,
                                        status="error", error=str(e))
            if downloaded:  # мог скачаться частично — всё равно освобождаем место
                hydration.track(file_id, path)
            return None
        if a.type == "review":
            # Точная копия уже разобранного файла едет туда же, куда оригинал.
            twin = self.db.conn.execute(
                "SELECT * FROM files WHERE sha256 = ? AND path != ? AND type IS NOT NULL AND type != 'review' "
                "AND status IN ('analyzed', 'moved') LIMIT 1", (a.sha256, str(path))).fetchone()
            if twin is not None:
                a.type, a.category, a.author, a.source = twin["type"], twin["category"], twin["author"], twin["source"]
                a.reason = f"копия {Path(twin['path']).name}"
        file_id = self.db.save_file(path, tags=a.tags, month=month, size=st.st_size, mtime=st.st_mtime,
                                    status="analyzed", error=None, **a.fields())
        if downloaded:
            hydration.track(file_id, path)
        return file_id

    def _with_retries(self, path: Path, action):
        for delay in (*self.retry_delays, None):
            try:
                return action()
            except (OSError, subprocess.TimeoutExpired) as e:
                if delay is None:
                    raise
                self.log(f"{path.name}: {e} — повтор через {delay:g} с")
                end = time.monotonic() + delay
                while time.monotonic() < end:
                    if self.should_stop and self.should_stop():
                        raise Stopped
                    time.sleep(min(0.5, delay))

    def target_for(self, row, filename: str) -> Path:
        parts = [self.cfg.folders[row["type"]]]
        if row["type"] == "art" and row["category"]:
            parts.append(row["category"])
        if self.cfg.by_month:
            month = row["month"] or (row["taken_at"] or "")[:7]
            if not month and row["mtime"]:
                month = datetime.fromtimestamp(row["mtime"]).strftime("%Y-%m")
            if month:
                parts.extend(month.split("/"))  # «2024/05» → подпапки 2024/05, как в источнике
        return self.cfg.target.joinpath(*parts, filename)

    # --- перенос и откат ---

    def apply(self, plan: list[PlannedMove]) -> int:
        """Переносит файлы по плану. Каждое действие сначала пишется в журнал."""
        batch = self.db.new_batch()
        done = 0
        for m in self._iter(plan, "Перенос"):
            if not m.src.exists():
                continue
            dst = unique_path(m.dst, set()) if m.dst.exists() else m.dst
            move_id = self.db.journal(batch, m.file_id, "move", m.src, dst)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m.src), str(dst))
            except OSError as e:
                self.log(f"Не удалось перенести {m.src.name}: {e}")
                self.db.journal_state(move_id, "failed")
                continue
            self.db.set_location(m.file_id, dst, "moved")
            self.db.journal_state(move_id, "done")
            if self._in_target(m.src):  # перекладка внутри результата: убираем опустевшие папки
                remove_empty_dirs(m.src.parent, self.cfg.target)
            done += 1
        self.log(f"Перенесено файлов: {done}. Номер переноса для отката: {batch}")
        return batch

    def undo(self, batch: int | None = None) -> int:
        batch = batch or self.db.last_move_batch()
        if batch is None:
            self.log("Откатывать нечего.")
            return 0
        restored = 0
        for m in self.db.moves_of(batch):
            if m["action"] != "move" or m["state"] != "done":
                continue
            src, dst = Path(m["src"]), Path(m["dst"])
            if not dst.exists() or src.exists():
                self.log(f"Пропуск (файл изменён вручную): {dst}")
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            self.db.set_location(m["file_id"], src, "moved" if self._in_target(src) else "analyzed")
            self.db.journal_state(m["id"], "undone")
            remove_empty_dirs(dst.parent, self.cfg.target)
            restored += 1
        self.log(f"Возвращено на место: {restored} (перенос №{batch})")
        return restored

    def _in_target(self, path: Path) -> bool:
        return self.cfg.target.resolve() in path.resolve().parents

    # --- пересчёт категорий по тегам из базы ---

    def recategorize(self) -> tuple[int, list[PlannedMove]]:
        """Применяет текущие списки тегов к уже разобранным артам без нейросети и без скачивания.

        Возвращает, у скольких артов сменилась категория, и план перекладки тех,
        что уже разложены по папкам. Ещё не разложенные просто получат новую категорию.
        """
        rows = self.db.conn.execute(
            "SELECT * FROM files WHERE type = 'art' AND status IN ('analyzed', 'moved')").fetchall()
        changed = 0
        plan: list[PlannedMove] = []
        reserved: set[Path] = set()
        for row in self._iter(rows, "Категории"):
            tags = {t["tag"]: t["score"] for t in self.db.tags_of(row["id"])
                    if t["kind"] in ("general", "character", "universe")}
            cats = match_categories(tags, self.cfg)
            category = cats[0] if cats else self.cfg.default_category
            self.db.set_category(row["id"], category, cats[1:])
            if category == row["category"]:
                continue
            changed += 1
            if row["status"] == "moved":
                current = Path(row["path"])
                dst = unique_path(self.target_for({**dict(row), "category": category}, current.name), reserved)
                reserved.add(dst)
                plan.append(PlannedMove(row["id"], current, dst, "art", category))
        return changed, plan

    # --- только отпечатки (для режима «Только дубли») ---

    def hash_library(self) -> int:
        """Считает хэши и отпечатки всей библиотеки (источник + результат), ничего не перенося."""
        roots = [r for r in (self.cfg.source, self.cfg.target) if r.exists()]
        files: list[Path] = []
        for r in roots:
            other = [x for x in roots if x != r]
            files += walk_media(r, other + [self.cfg.cache_dir])
        hydration = self._hydration()
        count = 0
        try:
            for path in self._iter(files, "Отпечатки"):
                count += self._hash_file(path, hydration)
        finally:
            hydration.release()
        return count

    def _hash_file(self, path: Path, hydration: Hydration) -> int:
        st = path.stat()
        row = self.db.get_by_path(path)
        if row is not None and row["sha256"] and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            return 0
        downloaded = hydration.before_read(path, st.st_size)
        try:
            info, sha, phash, _ = self._with_retries(path, lambda: hash_media(path, self.cfg))
        except Stopped:
            raise
        except Exception as e:
            self.errors += 1
            self.log(f"Ошибка: {path.name}: {e}")
            return 0
        status = row["status"] if row is not None and row["status"] != "error" else "hashed"
        file_id = self.db.save_file(path, size=st.st_size, mtime=st.st_mtime, kind=info.kind,
                                    width=info.width, height=info.height, duration=info.duration,
                                    taken_at=info.taken_at, sha256=sha, phash=phash, status=status)
        if downloaded:
            hydration.track(file_id, path)
        return 1


def unique_path(path: Path, reserved: set[Path]) -> Path:
    """IMG.jpg → IMG (1).jpg, если имя занято на диске или в текущем плане."""
    candidate, i = path, 1
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    return candidate


def remove_empty_dirs(folder: Path, stop: Path) -> None:
    stop = stop.resolve()
    folder = folder.resolve()
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def write_plan_csv(plan: list[PlannedMove], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["откуда", "куда", "тип", "категория"])
        for m in plan:
            w.writerow([m.src, m.dst, m.type, m.category or ""])
