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

from .analyze import is_io_error, Analyzer, Tagger, hash_media, match_categories
from .config import Config
from .db import Database
from .media import is_media
from .speed import DOWNLOAD, FILES, MOVE, Speedometer
from .onedrive import CloudFileRefused, CloudUnavailable, Hydration, explain_read_error, onedrive_running, start_onedrive


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
                 should_stop: Callable[[], bool] | None = None, speed: Speedometer | None = None):
        self.cfg = cfg
        self.speed = speed or Speedometer()
        self.db = db
        self.log = log
        # True — полоска в консоли, функция (подпись, сделано, всего) — для окна программы
        self.progress = progress
        self.should_stop = should_stop
        # Паузы перед повторами: OneDrive иногда не отдаёт файл с первого раза
        # (Errno 22, недокачанный файл), особенно пока освобождает место.
        self.retry_delays: tuple[float, ...] = (5, 30)
        # Если OneDrive закрылся, программа сама запускает его и ждёт до revive_timeout секунд.
        self.revive_timeout = 120.0
        self.revive_settle = 30.0
        self.max_revivals = 5  # подряд, без единого удачно прочитанного файла между ними
        self.revivals = 0
        self.refused_limit = 10  # столько файлов подряд OneDrive отказался отдать — будим его самого
        self.refused_streak = 0
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
            self.speed.add(FILES)
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

    def scan(self, month: str | None = None, full: bool = False, move: bool = False) -> list[PlannedMove]:
        """Разбирает новые файлы источника и возвращает план переноса.

        move=False — файлы не трогаются. move=True — каждая папка переносится сразу после разбора,
        чтобы сбой или остановка посреди большой библиотеки не теряли уже сделанное.
        Все папки одного запуска — один перенос в журнале, откатываются вместе."""
        plan: list[PlannedMove] = []
        reserved: set[Path] = set()
        batch: int | None = None
        moved = 0
        hydration = self._hydration()
        try:
            for done, (name, folder) in enumerate(self.months(month), 1):
                files = self._files_of(name, folder)
                folder_plan: list[PlannedMove] = []
                for path in self._iter(files, name or "(корень)"):
                    file_id = self._scan_file(path, name, full, hydration)
                    if file_id is None:
                        continue
                    row = self.db.get(file_id)
                    dst = unique_path(self.target_for(row, path.name), reserved)
                    reserved.add(dst)
                    folder_plan.append(PlannedMove(file_id, path, dst, row["type"], row["category"]))
                plan += folder_plan
                if move and folder_plan:
                    if batch is None:
                        batch = self.db.new_batch()
                    n = self._move(folder_plan, batch, hydration)
                    moved += n
                    self.log(f"{name or '(корень)'}: перенесено {n}")
                if done % self.cfg.onedrive_free_every == 0:
                    hydration.release()
        finally:
            hydration.release()
            if batch is not None:
                self.log(f"Всего перенесено файлов: {moved}. Номер переноса для отката: {batch}")
        if self.errors:
            self.log(f"Не удалось разобрать файлов: {self.errors}. Они останутся на месте; "
                     "при следующем запуске программа попробует их снова.")
        return plan

    def _scan_file(self, path: Path, month: str, full: bool, hydration: Hydration) -> int | None:
        # Уже разобранный файл узнаём по пути и размеру. Дату изменения не сравниваем: после
        # переустановки или сброса OneDrive она меняется в долях секунды, и всё скачивалось бы заново.
        st = path.stat()
        row = self.db.get_by_path(path)
        if row is not None and not full and row["status"] == "analyzed" and row["size"] == st.st_size:
            return row["id"]
        downloaded = hydration.before_read(path, st.st_size)
        try:
            a = self._with_retries(path, lambda: self.analyzer.analyze(path))
        except (Stopped, CloudUnavailable):
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
                a.base_type = twin["base_type"] or twin["type"]
                a.reason = f"копия {Path(twin['path']).name}"
        file_id = self.db.save_file(path, tags=a.tags, month=month, size=st.st_size, mtime=st.st_mtime,
                                    status="analyzed", error=None, **a.fields())
        if downloaded:
            self.speed.add(DOWNLOAD, st.st_size)
            hydration.track(file_id, path)
        return file_id

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.should_stop and self.should_stop():
                raise Stopped
            time.sleep(min(0.5, seconds))

    def _with_retries(self, path: Path, action):
        delays = list(self.retry_delays)
        cloud_failures = 0
        while True:
            try:
                result = action()
                self.revivals = 0
                self.refused_streak = 0
                return result
            except (OSError, subprocess.TimeoutExpired) as e:
                try:
                    reason = explain_read_error(path, e)
                except CloudFileRefused as refused:
                    self.refused_streak += 1
                    if self.refused_streak >= self.refused_limit:
                        # Столько файлов подряд — значит, дело не в файлах, а в самом OneDrive.
                        self.refused_streak = 0
                        if not self._revive_onedrive():
                            raise CloudUnavailable(str(refused)) from e
                    raise OSError(str(refused)) from e
                except CloudUnavailable:
                    cloud_failures += 1
                    if not self._revive_onedrive():
                        raise  # останавливает всю обработку
                    if cloud_failures == 1:
                        continue  # OneDrive снова работает — тот же файл ещё раз
                    # OneDrive падает снова на том же файле — пропускаем его, чтобы не ронять OneDrive по кругу.
                    raise OSError("OneDrive падает при скачивании этого файла, пропускаю его. "
                                  "Попробуйте открыть его в проводнике или на onedrive.live.com") from e
                if not delays or not is_io_error(e):  # «непонятный формат» ожиданием не исправить
                    raise OSError(reason) from e
                delay = delays.pop(0)
                self.log(f"{path.name}: {reason} — повтор через {delay:g} с")
                self._sleep(delay)

    def _revive_onedrive(self) -> bool:
        """OneDrive упал или не отвечает: запускает его и даёт время прийти в себя.

        Не проверяем тем же файлом: если OneDrive падает именно на нём, проверка уронила бы его снова."""
        if self.revivals >= self.max_revivals:
            return False
        self.revivals += 1
        if not onedrive_running():
            self.log("OneDrive упал — запускаю его заново")
            if not start_onedrive():
                self.log("Не нашёл OneDrive.exe, запустите OneDrive вручную")
                return False
            end = time.monotonic() + self.revive_timeout
            while not onedrive_running() and time.monotonic() < end:
                self._sleep(2)
        else:
            self.log("OneDrive не отдаёт файлы — жду, пока он придёт в себя")
        self._sleep(self.revive_settle)
        return True

    def target_for(self, row, filename: str) -> Path:
        parts = [self.cfg.folder_name(row["type"], row["category"])]
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
        done = self._move(plan, batch)
        self.log(f"Перенесено файлов: {done}. Номер переноса для отката: {batch}")
        return batch

    def _free_target(self, path: Path, file_id: int) -> Path:
        """Имя, не занятое ни на диске, ни в базе (там остаются пути дублей, удалённых в корзину)."""
        candidate, i = path, 1
        while candidate.exists() or self.db.path_taken(candidate, file_id):
            candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
            i += 1
        return candidate

    def finish_interrupted_moves(self) -> int:
        """Файл перенесён, но запись в базу сорвалась: дописываем, чтобы откат и отчёты его видели."""
        fixed = 0
        for m in self.db.pending_moves():
            src, dst = Path(m["src"]), Path(m["dst"])
            if dst.exists() and not src.exists():
                if self.db.path_taken(dst, m["file_id"]):
                    free = self._free_target(dst, m["file_id"])
                    dst.rename(free)
                    dst = free
                    self.db.set_move_dst(m["id"], dst)
                self.db.set_location(m["file_id"], dst, "moved")
                self.db.journal_state(m["id"], "done")
                fixed += 1
            else:
                self.db.journal_state(m["id"], "failed")
        if fixed:
            self.log(f"Дописал в базу прерванные переносы: {fixed}")
        return fixed

    def _move(self, plan: list[PlannedMove], batch: int, hydration: Hydration | None = None) -> int:
        done = 0
        self.finish_interrupted_moves()
        for m in self._iter(plan, "Перенос"):
            if not m.src.exists():
                continue
            dst = self._free_target(m.dst, m.file_id)
            move_id = self.db.journal(batch, m.file_id, "move", m.src, dst)
            try:
                size = m.src.stat().st_size
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m.src), str(dst))
            except OSError as e:
                self.log(f"Не удалось перенести {m.src.name}: {e}")
                self.db.journal_state(move_id, "failed")
                continue
            self.db.set_location(m.file_id, dst, "moved")
            self.db.journal_state(move_id, "done")
            self.speed.add(MOVE, size)
            if hydration is not None:
                hydration.moved(m.file_id, dst)
            if self._in_target(m.src):  # перекладка внутри результата: убираем опустевшие папки
                remove_empty_dirs(m.src.parent, self.cfg.target)
            done += 1
        return done

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
            if self.db.path_taken(src, m["file_id"]):
                self.log(f"Пропуск (на старом месте в базе уже другой файл): {src}")
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
        """Применяет текущие категории артов и свои папки к уже разобранным файлам
        без нейросети и без скачивания: по тегам и именам из базы.

        Возвращает, у скольких файлов сменилась папка, и план перекладки тех,
        что уже разложены. Ещё не разложенные просто получат новую папку.
        """
        rows = self.db.conn.execute(
            "SELECT * FROM files WHERE type IS NOT NULL AND status IN ('analyzed', 'moved')").fetchall()
        changed = 0
        plan: list[PlannedMove] = []
        reserved: set[Path] = set()
        for row in self._iter(rows, "Категории"):
            base = row["base_type"] or row["type"]
            if base == "custom":  # тип до своих папок неизвестен — не трогаем
                continue
            tags = {t["tag"]: t["score"] for t in self.db.tags_of(row["id"])
                    if t["kind"] in ("general", "character", "universe")}
            extra: list[str] = []
            if custom := self.cfg.match_custom(Path(row["orig_path"] or row["path"]).name, tags):
                new_type, category = "custom", custom.name
            elif base == "art":
                cats = match_categories(tags, self.cfg)
                new_type, category, extra = "art", cats[0] if cats else self.cfg.default_category, cats[1:]
            else:
                new_type, category = base, None
            same = new_type == row["type"] and category == row["category"]
            if not same or base == "art" or row["base_type"] is None:
                self.db.set_classification(row["id"], new_type, base, category, extra)
            if same:
                continue
            changed += 1
            if row["status"] == "moved":
                current = Path(row["path"])
                dst = unique_path(self.target_for({**dict(row), "type": new_type, "category": category},
                                                  current.name), reserved)
                reserved.add(dst)
                plan.append(PlannedMove(row["id"], current, dst, new_type, category))
        return changed, plan

    # --- только отпечатки (для режима «Только дубли») ---

    def hash_library(self, roots: list[Path] | None = None) -> int:
        """Считает хэши и отпечатки файлов, ничего не перенося.

        roots — папки для обхода; по умолчанию вся библиотека (источник + результат)."""
        if roots is None:
            roots = [self.cfg.source, self.cfg.target]
        roots = [r for r in roots if r.exists()]
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
        if row is not None and row["sha256"] and row["size"] == st.st_size:
            return 0
        downloaded = hydration.before_read(path, st.st_size)
        try:
            info, sha, phash, _ = self._with_retries(path, lambda: hash_media(path, self.cfg))
        except (Stopped, CloudUnavailable):
            raise
        except Exception as e:
            self.errors += 1
            self.log(f"Ошибка: {path.name}: {e}")
            return 0
        if downloaded:
            self.speed.add(DOWNLOAD, st.st_size)
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
