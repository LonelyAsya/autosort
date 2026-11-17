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
from .appdata import data_dir, ensure_config, open_path, set_path_setting, set_setting
from .config import Config, load_config
from .db import Database
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

        root.title(f"Autosort {__version__} — сортировка медиатеки")
        root.minsize(720, 560)
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

    # --- интерфейс ---

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        paths = ttk.LabelFrame(main, text="Папки", padding=8)
        paths.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        paths.columnconfigure(1, weight=1)
        self.source_var = tk.StringVar()
        self.target_var = tk.StringVar()
        for row, (label, var, key) in enumerate((
            ("Откуда:", self.source_var, "source"),
            ("Куда раскладывать:", self.target_var, "target"),
        )):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", **pad)
            ttk.Entry(paths, textvariable=var, state="readonly").grid(row=row, column=1, sticky="ew", **pad)
            ttk.Button(paths, text="Выбрать…", command=lambda k=key: self.choose_folder(k)).grid(
                row=row, column=2, **pad)

        sort = ttk.LabelFrame(main, text="Сортировка", padding=8)
        sort.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(sort, text="Год / месяц:").grid(row=0, column=0, sticky="w", **pad)
        self.month_var = tk.StringVar(value=ALL_MONTHS)
        self.month_box = ttk.Combobox(sort, textvariable=self.month_var, state="readonly", width=22)
        self.month_box.grid(row=0, column=1, sticky="w", **pad)
        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sort, text="Перепроверить уже разобранные", variable=self.full_var).grid(
            row=0, column=2, columnspan=2, sticky="w", **pad)
        self.by_month_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sort, text="Внутри категорий сохранять папки год/месяц",
                        variable=self.by_month_var, command=self.toggle_by_month).grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)

        self.buttons: list[ttk.Button] = []

        def button(parent, text, command, row, col):
            b = ttk.Button(parent, text=text, command=command)
            b.grid(row=row, column=col, sticky="ew", **pad)
            self.buttons.append(b)
            return b

        for col in range(4):
            sort.columnconfigure(col, weight=1)
        button(sort, "Пробный прогон", self.do_plan, 1, 0)
        button(sort, "Разложить", self.do_sort, 1, 1)
        button(sort, "Только сканировать", self.do_scan, 1, 2)
        button(sort, "Открыть результат", lambda: self.open_folder("target"), 1, 3)
        button(sort, "Пересчитать категории", self.do_recategorize, 2, 3)

        other = ttk.LabelFrame(main, text="Дубли, отчёт, журнал", padding=8)
        other.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for col in range(4):
            other.columnconfigure(col, weight=1)
        button(other, "Показать дубли", lambda: self.do_dupes(False), 0, 0)
        button(other, "Только дубли (вся библиотека)", lambda: self.do_dupes(True), 0, 1)
        button(other, "Сделать отчёт", self.do_report, 0, 2)
        button(other, "История переносов", self.do_history, 0, 3)
        button(other, "Откатить последний перенос", self.do_undo, 1, 0)
        button(other, "Настройки…", lambda: open_path(self.config_path), 1, 1)
        button(other, "Списки тегов…", lambda: open_path(self.folder / "rules"), 1, 2)
        button(other, "Перечитать настройки", self.reload_config, 1, 3)

        status = ttk.Frame(main)
        status.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(status, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        log_frame = ttk.Frame(main)
        log_frame.grid(row=4, column=0, sticky="nsew")
        main.rowconfigure(4, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
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
        self.events.put(("log", text))

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
                elif kind == "done":
                    self._set_busy(False)
                elif kind == "open":
                    open_path(event[1])
                elif kind == "months" and event[1] == self._months_source:
                    self.month_box.configure(values=[ALL_MONTHS, *event[2]])
        except queue.Empty:
            pass
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

    def open_folder(self, key: str) -> None:
        if self.cfg is None:
            return
        folder = self.cfg.source if key == "source" else self.cfg.target
        if folder.exists():
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
        self.status_var.set(title + "…")
        self.log(f"— {title} —")

        def worker():
            db = Database(cfg.database)
            try:
                job(cfg, db)
            except Stopped:
                self.log("Остановлено. Всё, что успели сделать, сохранено.")
            except Exception as e:
                self.log(f"Ошибка: {e}")
                self.log(traceback.format_exc())
            finally:
                db.close()
                self.events.put(("done",))

        threading.Thread(target=worker, daemon=True).start()

    def pipeline(self, cfg: Config, db: Database) -> Pipeline:
        return Pipeline(cfg, db, self.tagger, log=self.log, progress=self.report_progress,
                        should_stop=self.stop_flag.is_set)

    def _scan(self, cfg: Config, db: Database):
        month = self.month_var.get()
        pipe = self.pipeline(cfg, db)
        plan = pipe.scan(None if month == ALL_MONTHS else month, self.full_var.get())
        if not plan:
            self.log("Новых файлов нет.")
            return pipe, plan, ""
        lines = [f"Файлов: {len(plan)}"]
        for (t, cat), n in sorted(Counter((m.type, m.category) for m in plan).items(), key=lambda x: -x[1]):
            lines.append(f"  {cfg.folders[t]}{' / ' + cat if cat else ''}: {n}")
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
        def job(cfg, db):
            pipe, plan, summary = self._scan(cfg, db)
            if plan and self.ask(f"Перенести файлы?\n\n{summary}\n\nВсё можно будет откатить."):
                pipe.apply(plan)
        self.run_task("Сортировка", job)

    def do_recategorize(self) -> None:
        def job(cfg, db):
            pipe = self.pipeline(cfg, db)
            changed, plan = pipe.recategorize()
            self.log(f"Категории пересчитаны по тегам из базы. Изменилась у артов: {changed}")
            if not plan:
                return
            counts = Counter(m.category for m in plan)
            summary = "\n".join(f"  → {cat}: {n}" for cat, n in counts.most_common())
            self.log(f"Уже разложенных, которые поедут в другую папку: {len(plan)}\n{summary}")
            if self.ask(f"Переложить {len(plan)} уже разложенных артов в новые папки?\n\n{summary}\n\n"
                        "Это можно будет откатить."):
                pipe.apply(plan)
        self.run_task("Пересчёт категорий", job, need_source=False)

    def do_dupes(self, whole_library: bool) -> None:
        def job(cfg, db):
            from .review import create_server

            if whole_library:
                n = self.pipeline(cfg, db).hash_library()
                self.log(f"Новых отпечатков: {n}")
            created = create_server(cfg, db, log=self.log)
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
        self.run_task("Только дубли" if whole_library else "Поиск дублей", job, need_source=whole_library)

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
