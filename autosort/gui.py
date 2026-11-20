"""Окно программы: те же действия, что в командной строке, но кнопками."""
from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import webbrowser
from collections import Counter
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .appdata import data_dir, ensure_config, open_path, set_path_setting, set_setting, toml_value
from .settings_ui import CategoriesWindow, SettingsWindow
from .config import Config, load_config
from .db import Database
from .onedrive import CloudUnavailable
from . import updater
from .speed import Speedometer
from .pipeline import LazyTagger, Pipeline, Stopped, folder_choices, media_folders, write_plan_csv

ALL_MONTHS = "Все папки"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.folder = data_dir()
        self.config_path = ensure_config(self.folder)
        self.cfg: Config | None = None
        self.tagger: LazyTagger | None = None
        self.events: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.server = None
        self.busy = False
        self._tagger_key = None
        self._months_source: Path | None = None
        self.speed = Speedometer()

        root.title(f"Autosort {__version__} — сортировка медиатеки")
        root.minsize(900, 720)
        try:
            icon = resources.files("autosort").joinpath("data", "icon.png").read_bytes()
            self._icon = tk.PhotoImage(data=icon)
            root.iconphoto(True, self._icon)
            if os.name == "nt":  # на Windows заголовок окна надёжнее берёт .ico
                root.iconbitmap(default=str(resources.files("autosort").joinpath("data", "autosort.ico")))
        except Exception:
            pass
        self._build()
        root.report_callback_exception = self._on_error
        self.reload_config()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)
        if self.cfg is None or self.cfg.check_updates:
            root.after(3000, self.check_updates)

    # --- интерфейс ---

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        self.buttons: list[ttk.Button] = []

        def button(parent, text, command, row, col, span=1):
            b = ttk.Button(parent, text=text, command=command)
            b.grid(row=row, column=col, columnspan=span, sticky="ew", **pad)
            self.buttons.append(b)
            return b

        def block(title: str, row: int, columns: int) -> ttk.LabelFrame:
            frame = ttk.LabelFrame(main, text=title, padding=6)
            frame.grid(row=row, column=0, sticky="ew", pady=(0, 6))
            for col in range(columns):
                frame.columnconfigure(col, weight=1)
            return frame

        def folder_row(parent, row, label, var, command):
            box = ttk.Frame(parent)
            box.grid(row=row, column=0, columnspan=4, sticky="ew")
            box.columnconfigure(1, weight=1)
            ttk.Label(box, text=label, width=20).grid(row=0, column=0, sticky="w", **pad)
            ttk.Entry(box, textvariable=var, state="readonly").grid(row=0, column=1, sticky="ew", **pad)
            b = ttk.Button(box, text="Выбрать", command=command)
            b.grid(row=0, column=2, **pad)
            self.buttons.append(b)

        paths = block("Папки", 0, 4)
        self.source_var = tk.StringVar()
        self.target_var = tk.StringVar()
        folder_row(paths, 0, "Откуда:", self.source_var, lambda: self.choose_folder("source"))
        folder_row(paths, 1, "Куда раскладывать:", self.target_var, lambda: self.choose_folder("target"))

        sort = block("Сортировка", 1, 4)
        ttk.Label(sort, text="Год / месяц:").grid(row=0, column=0, sticky="w", **pad)
        self.month_var = tk.StringVar(value=ALL_MONTHS)
        self.month_box = ttk.Combobox(sort, textvariable=self.month_var, state="readonly", width=22)
        self.month_box.grid(row=0, column=1, sticky="w", **pad)
        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sort, text="Перепроверить уже разобранные", variable=self.full_var).grid(
            row=0, column=2, columnspan=2, sticky="w", **pad)
        button(sort, "Пробный прогон", self.do_plan, 1, 0)
        button(sort, "Разложить", self.do_sort, 1, 1)
        button(sort, "Только сканировать", self.do_scan, 1, 2)
        button(sort, "Открыть результат", lambda: self.open_folder("target"), 1, 3)
        self.by_month_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sort, text="Внутри категорий сохранять папки год/месяц",
                        variable=self.by_month_var, command=self.toggle_by_month).grid(
            row=2, column=0, columnspan=2, sticky="w", **pad)
        button(sort, "Проверка на дубли в конечной папке", self.do_dupes_target, 2, 2)
        button(sort, "Пересчитать категории", self.do_recategorize, 2, 3)

        dupes = block("Дубли", 2, 4)
        self.dupes_var = tk.StringVar()
        folder_row(dupes, 0, "Где искать дубли:", self.dupes_var, self.choose_dupes_folder)
        button(dupes, "Проверка на дубли", self.do_dupes_folder, 1, 0)
        button(dupes, "Отчёт по дублям", self.do_dupes_report, 1, 1)
        button(dupes, "Откат удаления дублей", self.do_undo_trash, 1, 2)
        button(dupes, "Открыть папку", lambda: self.open_folder("dupes"), 1, 3)

        report = block("Отчёт", 3, 3)
        button(report, "Сделать отчёт", self.do_report, 0, 0)
        button(report, "История переносов", self.do_history, 0, 1)
        button(report, "Откатить последний перенос", self.do_undo, 0, 2)

        program = block("Программа", 4, 5)
        button(program, "Настройки", self.open_settings, 0, 0)
        button(program, "Категории и теги", self.open_categories, 0, 1)
        button(program, "Перечитать настройки", self.reload_config, 0, 2)
        button(program, "Проверить обновления", lambda: self.check_updates(manual=True), 0, 3)
        button(program, "Удалить программу", self.uninstall, 0, 4)

        status = ttk.Frame(main)
        status.grid(row=5, column=0, sticky="ew", pady=(0, 4))
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.speed_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.speed_var, foreground="#555").grid(row=2, column=0, columnspan=2, sticky="w")
        self.stop_button = ttk.Button(status, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        log_frame = ttk.Frame(main)
        log_frame.grid(row=6, column=0, sticky="nsew")
        main.rowconfigure(6, weight=1)
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _on_error(self, exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        self.log(text)
        messagebox.showerror("Autosort", f"Ошибка: {exc}\n\nПодробности в журнале внизу окна.")

    # --- связь с фоновой задачей (tkinter можно трогать только из главного потока) ---

    def log(self, text: str) -> None:
        # Время в журнале и копия в autosort.log: по ним видно, что происходило, когда закрылся OneDrive.
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}", flush=True)
        self.events.put(("log", f"{datetime.now():%H:%M:%S}  {text}"))

    def report_progress(self, label: str, done: int, total: int) -> None:
        self.events.put(("progress", label, done, total))

    def ask(self, question: str) -> bool:
        """Вопрос пользователю из фоновой задачи: ждёт ответа в главном окне."""
        answer = {"event": threading.Event(), "value": False}
        self.events.put(("ask", question, answer))
        answer["event"].wait()
        return answer["value"]

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", event[1] + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    _, label, done, total = event
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"{label}: {done} из {total}")
                elif kind == "ask":
                    _, question, answer = event
                    answer["value"] = messagebox.askyesno("Autosort", question, parent=self.root)
                    answer["event"].set()
                elif kind == "update":
                    self._offer_update(*event[1:])
                elif kind == "quit":
                    self.root.destroy()
                    return
                elif kind == "warn":
                    messagebox.showwarning("Autosort", event[1], parent=self.root)
                elif kind == "done":
                    self._set_busy(False)
                elif kind == "open":
                    open_path(event[1])
                elif kind == "months" and event[1] == self._months_source:
                    self.month_box.configure(values=[ALL_MONTHS, *event[2]])
        except queue.Empty:
            pass
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 10 == 0:
            self.speed_var.set(self.speed.text() if self.busy else "")
        self.root.after(100, self._poll)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for b in self.buttons:
            b.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.server = None
            self.status_var.set("Готово")
            self.progress.configure(value=0)

    # --- настройки ---

    def reload_config(self) -> bool:
        try:
            self.cfg = load_config(self.config_path)
        except Exception as e:
            self.cfg = None
            messagebox.showerror("Autosort", f"Ошибка в настройках {self.config_path}:\n\n{e}")
            return False
        cfg = self.cfg
        tagger_key = (cfg.tagger_enabled, cfg.tagger_model, cfg.tagger_device,
                      cfg.general_threshold, cfg.character_threshold)
        if self.tagger is None or tagger_key != self._tagger_key:  # модель не перезагружаем зря
            self.tagger = LazyTagger(cfg, self.log)
            self._tagger_key = tagger_key
        self.source_var.set(str(cfg.source))
        self.target_var.set(str(cfg.target))
        self.dupes_var.set(str(cfg.dupes_folder) if cfg.dupes_folder else "(не выбрана)")
        self.by_month_var.set(cfg.by_month)
        if cfg.source != self._months_source:
            self._months_source = cfg.source
            self._refresh_months(cfg)
        return True

    def _refresh_months(self, cfg: Config) -> None:
        """Список год/месяц собирается в фоне: обход большой папки OneDrive может занять время."""
        self.month_box.configure(values=[ALL_MONTHS])
        self.month_var.set(ALL_MONTHS)

        def worker():
            try:
                months = [m for m in folder_choices(media_folders(cfg)) if m] if cfg.source.is_dir() else []
            except OSError as e:
                self.log(f"Не удалось прочитать папку {cfg.source}: {e}")
                months = []
            self.events.put(("months", cfg.source, months))

        threading.Thread(target=worker, daemon=True).start()

    def choose_folder(self, key: str) -> None:
        current = self.source_var.get() if key == "source" else self.target_var.get()
        initial = current if Path(current).is_dir() else str(Path.home())
        folder = filedialog.askdirectory(parent=self.root, initialdir=initial, mustexist=key == "source",
                                         title="Откуда брать файлы" if key == "source" else "Куда раскладывать")
        if folder:
            set_path_setting(self.config_path, key, folder)
            self.reload_config()

    def toggle_by_month(self) -> None:
        set_setting(self.config_path, "layout", "by_month", "true" if self.by_month_var.get() else "false")
        self.reload_config()

    def choose_dupes_folder(self) -> None:
        current = self.cfg.dupes_folder if self.cfg else None
        folder = filedialog.askdirectory(parent=self.root, mustexist=True, title="В какой папке искать дубли?",
                                         initialdir=str(current if current and current.is_dir() else Path.home()))
        if folder:
            set_setting(self.config_path, "dupes", "folder", toml_value(Path(folder).as_posix()))
            self.reload_config()

    def open_settings(self) -> None:
        if self.busy:
            messagebox.showinfo("Autosort", "Дождитесь окончания задачи или остановите её.", parent=self.root)
        elif self.reload_config():
            SettingsWindow(self)

    def open_categories(self) -> None:
        if self.busy:
            messagebox.showinfo("Autosort", "Дождитесь окончания задачи или остановите её.", parent=self.root)
        elif self.reload_config():
            CategoriesWindow(self)

    def open_folder(self, key: str) -> None:
        if self.cfg is None:
            return
        folder = {"source": self.cfg.source, "target": self.cfg.target, "dupes": self.cfg.dupes_folder}[key]
        if folder is None:
            messagebox.showinfo("Autosort", "Сначала выберите папку кнопкой «Выбрать».")
        elif folder.exists():
            open_path(folder)
        else:
            messagebox.showinfo("Autosort", "Папки пока нет: в ней ещё ничего не разложено.")

    # --- запуск задач ---

    def run_task(self, title: str, job: Callable[[Config, Database], None], need_source: bool = True) -> None:
        if self.busy or not self.reload_config():
            return
        cfg = self.cfg
        if need_source and not cfg.source.is_dir():
            messagebox.showwarning("Autosort", "Сначала выберите папку «Откуда».")
            return
        self._set_busy(True)
        self.stop_flag.clear()
        self.speed.reset()
        self.status_var.set(title + "…")
        self.log(f"— {title} —")

        def worker():
            db = Database(cfg.database)
            try:
                job(cfg, db)
            except Stopped:
                self.log("Остановлено. Всё, что успели сделать, сохранено.")
            except CloudUnavailable as e:
                self.log(str(e))
                self.events.put(("warn", str(e)))
            except Exception as e:
                self.log(f"Ошибка: {e}")
                self.log(traceback.format_exc())
            finally:
                db.close()
                self.events.put(("done",))

        threading.Thread(target=worker, daemon=True).start()

    def pipeline(self, cfg: Config, db: Database) -> Pipeline:
        return Pipeline(cfg, db, self.tagger, log=self.log, progress=self.report_progress,
                        should_stop=self.stop_flag.is_set, speed=self.speed)

    def _scan(self, cfg: Config, db: Database, move: bool = False):
        month = self.month_var.get()
        pipe = self.pipeline(cfg, db)
        plan = pipe.scan(None if month == ALL_MONTHS else month, self.full_var.get(), move=move)
        if not plan:
            self.log("Новых файлов нет.")
            return pipe, plan, ""
        lines = [f"Файлов: {len(plan)}"]
        for (t, cat), n in sorted(Counter((m.type, m.category) for m in plan).items(), key=lambda x: -x[1]):
            lines.append(f"  {cfg.folder_name(t, cat)}{' / ' + cat if cat and t != 'custom' else ''}: {n}")
        summary = "\n".join(lines)
        self.log(summary)
        return pipe, plan, summary

    def do_scan(self) -> None:
        self.run_task("Сканирование", lambda cfg, db: self._scan(cfg, db))

    def do_plan(self) -> None:
        def job(cfg, db):
            _, plan, _ = self._scan(cfg, db)
            if plan:
                out = cfg.base_dir / "plan.csv"
                write_plan_csv(plan, out)
                self.log(f"Подробный план: {out}")
                if self.ask("Пробный прогон готов, файлы не тронуты.\n\nОткрыть подробный план (таблицу)?"):
                    self.events.put(("open", out))
        self.run_task("Пробный прогон", job)

    def do_sort(self) -> None:
        if not messagebox.askyesno(
                "Autosort", "Разложить файлы?\n\nПрограмма разбирает папки по очереди и переносит каждую "
                "сразу после разбора, так что при сбое или остановке уже разложенное остаётся разложенным.\n\n"
                "Посмотреть заранее, что куда поедет, можно «Пробным прогоном». Всё можно откатить кнопкой "
                "«Откатить последний перенос».", parent=self.root):
            return

        def job(cfg, db):
            self._scan(cfg, db, move=True)
        self.run_task("Сортировка", job)

    def do_recategorize(self) -> None:
        def job(cfg, db):
            pipe = self.pipeline(cfg, db)
            changed, plan = pipe.recategorize()
            self.log(f"Категории и свои папки пересчитаны по тегам из базы. Папка сменилась у файлов: {changed}")
            if not plan:
                return
            counts = Counter(cfg.folder_name(m.type, m.category) + (f" / {m.category}" if m.type == "art" else "")
                             for m in plan)
            summary = "\n".join(f"  → {cat}: {n}" for cat, n in counts.most_common())
            self.log(f"Уже разложенных, которые поедут в другую папку: {len(plan)}\n{summary}")
            if self.ask(f"Переложить {len(plan)} уже разложенных файлов в новые папки?\n\n{summary}\n\n"
                        "Это можно будет откатить."):
                pipe.apply(plan)
        self.run_task("Пересчёт категорий", job, need_source=False)

    def do_dupes(self, folder: Path) -> None:
        """Отпечатки всех файлов папки и окно разбора дублей в браузере."""
        def job(cfg, db):
            from .review import create_server

            n = self.pipeline(cfg, db).hash_library([folder])
            self.log(f"Новых отпечатков: {n}")
            created = create_server(cfg, db, log=self.log, roots=[folder])
            if created is None:
                return
            self.server, url = created
            webbrowser.open(url)
            self.log("Разбор дублей открыт в браузере. Когда закончите — «Закончить» там или «Остановить» здесь.")
            self.events.put(("progress", "Идёт разбор дублей в браузере", 0, 1))
            try:
                self.server.serve_forever()
            finally:
                self.server.server_close()
        self.run_task(f"Дубли в {folder}", job, need_source=False)

    def _dupes_folder(self) -> Path | None:
        if not self.reload_config():
            return None
        folder = self.cfg.dupes_folder
        if folder is None or not folder.is_dir():
            messagebox.showwarning("Autosort", "Сначала выберите папку в блоке «Дубли» кнопкой «Выбрать».")
            return None
        return folder

    def do_dupes_folder(self) -> None:
        if not self.busy and (folder := self._dupes_folder()):
            self.do_dupes(folder)

    def do_dupes_target(self) -> None:
        if self.busy or not self.reload_config():
            return
        if not self.cfg.target.is_dir():
            messagebox.showinfo("Autosort", "В папке «Куда раскладывать» пока ничего нет.")
            return
        self.do_dupes(self.cfg.target)

    def do_dupes_report(self) -> None:
        if self.busy or not (folder := self._dupes_folder()):
            return

        def job(cfg, db):
            from .dupes import dupes_report, find_groups

            self.pipeline(cfg, db).hash_library([folder])
            groups = find_groups(db, cfg.phash_distance, [folder])
            out = cfg.base_dir / "dupes_report.txt"
            out.write_text(dupes_report(db, groups, folder), encoding="utf-8")
            self.log(f"Групп дублей: {len(groups)}. Отчёт: {out}")
            self.events.put(("open", out))
        self.run_task("Отчёт по дублям", job, need_source=False)

    def do_undo_trash(self) -> None:
        def job(cfg, db):
            from .dupes import undo_trash

            batch = db.last_trash_batch()
            if batch is None:
                self.log("Удалений дублей в журнале нет.")
            elif self.ask(f"Вернуть из корзины файлы, удалённые при разборе дублей №{batch}?"):
                undo_trash(db, batch, cfg.target, self.log)
        self.run_task("Откат удаления дублей", job, need_source=False)

    def do_report(self) -> None:
        def job(cfg, db):
            from .report import build_report

            out = cfg.base_dir / "report.txt"
            out.write_text(build_report(cfg, db), encoding="utf-8")
            self.log(f"Отчёт сохранён: {out}")
            self.events.put(("open", out))
        self.run_task("Отчёт", job, need_source=False)

    def do_history(self) -> None:
        def job(cfg, db):
            rows = db.batches()
            if not rows:
                self.log("Журнал пуст.")
            for r in rows:
                action = "перенос" if r["action"] == "move" else "в корзину"
                undone = f", откачено {r['undone']}" if r["undone"] else ""
                self.log(f"№{r['batch']}  {r['at'].replace('T', ' ')}  {action}: {r['done']} из {r['n']}{undone}")
        self.run_task("История", job, need_source=False)

    def do_undo(self) -> None:
        def job(cfg, db):
            batch = db.last_move_batch()
            if batch is None:
                self.log("Откатывать нечего.")
            elif self.ask(f"Вернуть файлы переноса №{batch} на старые места?"):
                self.pipeline(cfg, db).undo(batch)
        self.run_task("Откат", job, need_source=False)

    def stop(self) -> None:
        self.stop_flag.set()
        self.status_var.set("Останавливаю…")
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    # --- обновление и удаление ---

    def check_updates(self, manual: bool = False) -> None:
        def worker():
            try:
                release = updater.latest_release()
            except Exception as e:
                if manual:
                    self.events.put(("warn", f"Не удалось проверить обновления: {e}"))
                else:
                    print(f"проверка обновлений не удалась: {e}", flush=True)
                return
            if updater.is_newer(release.version):
                self.events.put(("update", release, manual))
            elif manual:
                self.log(f"Установлена последняя версия ({__version__}).")

        threading.Thread(target=worker, daemon=True).start()

    def _offer_update(self, release: updater.Release, manual: bool) -> None:
        self.log(f"Вышла версия {release.version} (у вас {__version__}).")
        if self.busy and not manual:
            self.log("Обновиться можно кнопкой «Проверить обновления», когда закончится задача.")
            return
        notes = updater.short_notes(release.notes)
        folder = updater.installed_dir()
        text = f"Вышла версия {release.version} (у вас {__version__}).\n\nЧто нового:\n{notes}\n\n"
        if folder is None or not release.setup_url:
            if messagebox.askyesno("Обновление Autosort", text + "Открыть страницу загрузки?", parent=self.root):
                webbrowser.open(release.page)
            return
        if self.busy:
            messagebox.showinfo("Обновление Autosort", text + "Обновиться можно, когда закончится текущая задача.",
                                parent=self.root)
            return
        if messagebox.askyesno("Обновление Autosort", text + "Скачать и установить сейчас? Программа закроется "
                               "и откроется уже новой версией, настройки и база сохранятся.", parent=self.root):
            self._install_update(release, folder)

    def _install_update(self, release: updater.Release, folder: Path) -> None:
        self._set_busy(True)
        self.stop_flag.clear()
        self.log(f"— Обновление до {release.version} —")
        mb = 1024 * 1024

        def progress(done: int, total: int) -> None:
            self.events.put(("progress", f"Скачиваю {release.version}", done // mb, max(total // mb, 1)))

        def worker():
            try:
                setup = updater.download(release, progress, self.stop_flag.is_set)
                self.log("Запускаю установщик, программа сейчас закроется…")
                updater.run_installer(setup, folder)
                self.events.put(("quit",))
            except InterruptedError:
                self.log("Обновление отменено.")
                self.events.put(("done",))
            except Exception as e:
                self.log(f"Не удалось обновиться: {e}")
                self.events.put(("done",))

        threading.Thread(target=worker, daemon=True).start()

    def uninstall(self) -> None:
        folder = updater.installed_dir()
        if folder is None:
            here = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
            text = ("Это портативная версия: чтобы удалить её, закройте программу и удалите её папку"
                    + (f":\n{here}" if here else ".") +
                    f"\n\nНастройки и база лежат в {self.folder}.")
            messagebox.showinfo("Удаление Autosort", text, parent=self.root)
            return
        if self.busy:
            messagebox.showinfo("Удаление Autosort", "Сначала дождитесь окончания задачи или остановите её.",
                                parent=self.root)
            return
        if messagebox.askyesno("Удаление Autosort", "Удалить Autosort с компьютера?\n\nВаши фото и разложенные "
                               "папки не затрагиваются. В конце удаления можно будет выбрать, стереть ли "
                               "настройки, базу и нейросеть.", parent=self.root):
            updater.run_uninstaller(folder)
            self.root.destroy()

    def on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Autosort", "Задача ещё идёт. Остановить и выйти?\n"
                                       "Уже сделанное сохранится."):
                return
            self.stop()
        self.root.destroy()


def main() -> None:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # У exe без консоли нет stdout/stderr — пишем всё в файл, чтобы ничего не падало.
    if sys.stdout is None or sys.stderr is None:
        log_file = open(folder / "autosort.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log_file
        sys.stderr = sys.stderr or log_file
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} запуск Autosort {__version__}: {sys.executable}", flush=True)
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    if os.name == "nt":
        try:  # чёткий шрифт на мониторах с масштабированием
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    App(root)
    try:  # заставка из exe: закрываем, когда окно уже готово
        import pyi_splash  # type: ignore[import-not-found]

        pyi_splash.close()
    except ImportError:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
