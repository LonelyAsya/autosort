"""Окна «Настройки» и «Категории и теги»: всё то же, что в config.toml и файлах тегов, но без ручной правки файлов."""
from __future__ import annotations

import csv
import threading
import tomllib
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .appdata import open_path, safe_file_name, save_categories, save_custom_folders, set_setting, toml_value
from .config import Config, normalize_tag

PAD = {"padx": 8, "pady": 3}
HINT = {"foreground": "#666"}


def _window(app, title: str) -> tk.Toplevel:
    win = tk.Toplevel(app.root)
    win.title(title)
    win.transient(app.root)
    win.grab_set()
    try:
        win.iconphoto(False, app._icon)
    except Exception:
        pass
    return win


class SettingsWindow:
    """Настройки по вкладкам. Сохраняет только изменённые значения, комментарии в файле остаются."""

    DEVICES = {"gpu": "Видеокарта (быстрее)", "cpu": "Процессор (если с видеокартой проблемы)"}

    def __init__(self, app):
        self.app = app
        cfg: Config = app.cfg
        self.win = _window(app, "Настройки Autosort")
        self.fields: list[tuple[str, str, tk.Variable, str, object]] = []
        book = ttk.Notebook(self.win)
        book.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        tab = self._tab(book, "Раскладка")
        self._section(tab, "Названия папок в «Куда раскладывать»")
        for key, title in (("camera", "Фото с камеры"), ("art", "Арты"), ("meme", "Мемы"),
                           ("screenshot", "Скриншоты"), ("review", "Ручной разбор")):
            self._entry(tab, title, "folders", key, cfg.folders[key])
        self._check(tab, "Внутри папок сохранять подпапки год/месяц", "layout", "by_month", cfg.by_month)

        self._custom_tab(self._tab(book, "Свои папки"))

        tab = self._tab(book, "Нейросеть")
        self._check(tab, "Подписывать арты тегами нейросетью", "tagger", "enabled", cfg.tagger_enabled)
        self._choice(tab, "На чём считать", "tagger", "device", cfg.tagger_device, self.DEVICES)
        self._number(tab, "Уверенность для обычных тегов", "tagger", "general_threshold",
                     cfg.general_threshold, 0.05, 0.95, 0.05,
                     "Меньше — больше тегов, но чаще ошибочные. Обычно 0,35.")
        self._number(tab, "Уверенность для персонажей", "tagger", "character_threshold",
                     cfg.character_threshold, 0.3, 0.99, 0.05,
                     "Меньше — модель чаще узнаёт персонажей, но и чаще путает. Обычно 0,75–0,85.")
        self._number(tab, "Кадров из видео и GIF", "tagger", "video_frames", cfg.video_frames, 1, 20, 1, None, int)
        self._number(tab, "Уверенность «это арт»", "classify", "art_threshold", cfg.art_threshold, 0.1, 0.95, 0.05)
        self._number(tab, "Уверенность «это мем»", "classify", "meme_threshold", cfg.meme_threshold, 0.1, 0.95, 0.05,
                     "Новые пороги действуют на новые файлы. Чтобы применить их к старым, "
                     "запустите разбор с галочкой «Перепроверить уже разобранные».")

        tab = self._tab(book, "Дубли и OneDrive")
        self._number(tab, "Насколько похожими считать картинки", "dupes", "phash_distance", cfg.phash_distance,
                     0, 16, 1, "0 — только одинаковые, 6–8 — пересжатые и чуть обрезанные копии, "
                     "больше — риск ложных совпадений.", int)
        self._check(tab, "После обработки возвращать файлы «только в облако»", "onedrive", "free_space",
                    cfg.onedrive_free_space)
        self._number(tab, "Освобождать место через каждые N месяцев", "onedrive", "free_every",
                     cfg.onedrive_free_every, 1, 24, 1, None, int)
        self._number(tab, "Держать свободными на диске, ГБ", "onedrive", "reserve_gb",
                     round(cfg.onedrive_reserve_bytes / 1024**3, 1), 1, 200, 1,
                     "Если места меньше, оно освобождается сразу, не дожидаясь конца месяца.")

        tab = self._tab(book, "Прочее")
        self._check(tab, "Проверять обновления при запуске", "app", "check_updates", cfg.check_updates)
        self._section(tab, "Поиск авторов через SauceNAO (картинки уходят на внешний сервис)")
        self._check(tab, "Включить SauceNAO", "authors.saucenao", "enabled", cfg.saucenao_enabled)
        self._entry(tab, "Ключ API", "authors.saucenao", "api_key", cfg.saucenao_api_key, secret=True)
        self._number(tab, "Минимальное сходство, %", "authors.saucenao", "min_similarity",
                     cfg.saucenao_min_similarity, 50, 99, 1)

        bottom = ttk.Frame(self.win, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Открыть файл настроек (для опытных)",
                   command=lambda: open_path(app.config_path)).pack(side="left")
        ttk.Button(bottom, text="Сохранить", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Отмена", command=self.win.destroy).pack(side="right", padx=8)

    # --- свои папки верхнего уровня ---

    def _custom_tab(self, tab) -> None:
        raw = tomllib.loads(self.app.config_path.read_text(encoding="utf-8"))
        self.custom = []
        for c in raw.get("custom_folders", []):
            path = self.app.cfg.base_dir / c["tags_file"] if c.get("tags_file") else None
            self.custom.append({"name": c["name"], "tags_file": c.get("tags_file"),
                                "patterns": list(c.get("patterns", [])),
                                "text": path.read_text(encoding="utf-8") if path and path.exists() else ""})
        self.custom_dirty = False
        ttk.Label(tab, text="Папки рядом с «Камерой», «Артами», «Мемами» и т. д. Файл попадает в первую папку, "
                  "у которой совпала маска имени или хоть один тег нейросети. Свои папки проверяются раньше "
                  "встроенных, сверху вниз. Теги ставятся артам, мемам и файлам для ручного разбора; "
                  "фото с камеры и скриншоты попадают в свои папки только по маске имени.",
                  wraplength=520, **HINT).grid(row=0, column=0, columnspan=2, sticky="w", **PAD)
        self.custom_list = tk.Listbox(tab, height=8, exportselection=False)
        self.custom_list.grid(row=1, column=0, sticky="nsew", **PAD)
        self.custom_list.bind("<Double-Button-1>", lambda e: self._edit_custom())
        side = ttk.Frame(tab)
        side.grid(row=1, column=1, sticky="n", **PAD)
        for text, cmd in (("Добавить", self._add_custom), ("Изменить", self._edit_custom),
                          ("Удалить", self._remove_custom), ("▲ Выше", lambda: self._move_custom(-1)),
                          ("▼ Ниже", lambda: self._move_custom(1))):
            ttk.Button(side, text=text, command=cmd).pack(fill="x", pady=2)
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=0)
        self._fill_custom()

    def _fill_custom(self, select: int | None = None) -> None:
        self.custom_list.delete(0, "end")
        for i, c in enumerate(self.custom, 1):
            rules = []
            if c["patterns"]:
                rules.append("имена: " + ", ".join(c["patterns"]))
            n = sum(1 for line in c["text"].splitlines() if line.strip() and not line.lstrip().startswith("#"))
            if n:
                rules.append(f"тегов: {n}")
            self.custom_list.insert("end", f"{i}. {c['name']}  ({'; '.join(rules) or 'правил нет'})")
        if select is not None and self.custom:
            self.custom_list.selection_set(max(0, min(select, len(self.custom) - 1)))

    def _selected_custom(self) -> int | None:
        sel = self.custom_list.curselection()
        return sel[0] if sel else None

    def _custom_dialog(self, item: dict | None) -> dict | None:
        dlg = tk.Toplevel(self.win)
        dlg.title("Своя папка")
        dlg.transient(self.win)
        dlg.grab_set()
        frame = ttk.Frame(dlg, padding=10)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        name = tk.StringVar(value=item["name"] if item else "")
        masks = tk.StringVar(value=", ".join(item["patterns"]) if item else "")
        ttk.Label(frame, text="Название папки:").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(frame, textvariable=name)
        name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(frame, text="Маски имён файлов через запятую (необязательно):").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=masks).grid(row=3, column=0, sticky="ew")
        ttk.Label(frame, text="* — любые символы. Например: *telegram*, IMG_*.heic, *wallpaper*",
                  **HINT).grid(row=4, column=0, sticky="nw", pady=(0, 8))
        tags_box = ttk.LabelFrame(frame, text="Теги нейросети, по одному на строку (необязательно)", padding=4)
        tags_box.grid(row=5, column=0, sticky="nsew")
        tags_box.columnconfigure(0, weight=1)
        tags_box.rowconfigure(0, weight=1)
        text = tk.Text(tags_box, width=50, height=10, undo=True)
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", item["text"] if item else "")
        ttk.Label(frame, text="Названия тегов можно подсмотреть в «Категории и теги» → «Найти тег».",
                  **HINT).grid(row=6, column=0, sticky="w")
        result: dict = {}

        def ok():
            n = name.get().strip()
            if not n:
                messagebox.showerror("Своя папка", "Укажите название папки.", parent=dlg)
                return
            builtin = {var.get().strip().lower() for sec, _, var, _, _ in self.fields if sec == "folders"}
            others = {c["name"].lower() for c in self.custom if c is not item}
            if n.lower() in builtin | others:
                messagebox.showerror("Своя папка", f"Папка «{n}» уже есть.", parent=dlg)
                return
            patterns = [m.strip() for m in masks.get().split(",") if m.strip()]
            body = text.get("1.0", "end-1c")
            if not patterns and not any(line.strip() and not line.lstrip().startswith("#")
                                        for line in body.splitlines()):
                messagebox.showerror("Своя папка", "Добавьте хотя бы маску имени или один тег — "
                                     "иначе в папку ничего не попадёт.", parent=dlg)
                return
            result.update(name=n, patterns=patterns, text=body)
            dlg.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="right")
        ttk.Button(buttons, text="Готово", command=ok).pack(side="right", padx=8)
        name_entry.focus_set()
        dlg.wait_window()
        return result or None

    def _add_custom(self) -> None:
        data = self._custom_dialog(None)
        if not data:
            return
        used = {c["tags_file"] for c in self.custom}
        stem, n = "папка " + safe_file_name(data["name"]), 1
        file = f"rules/{stem}.txt"
        while file in used or (self.app.cfg.base_dir / file).exists():
            n += 1
            file = f"rules/{stem} {n}.txt"
        self.custom.append({**data, "tags_file": file})
        self.custom_dirty = True
        self._fill_custom(len(self.custom) - 1)

    def _edit_custom(self) -> None:
        i = self._selected_custom()
        if i is None:
            return
        data = self._custom_dialog(self.custom[i])
        if data:
            self.custom[i].update(data)
            self.custom_dirty = True
            self._fill_custom(i)

    def _remove_custom(self) -> None:
        i = self._selected_custom()
        if i is not None and messagebox.askyesno(
                "Свои папки", f"Убрать папку «{self.custom[i]['name']}» из правил?\n\nУже разложенные файлы "
                "останутся на месте, пока вы не нажмёте «Пересчитать категории».", parent=self.win):
            del self.custom[i]
            self.custom_dirty = True
            self._fill_custom(i)

    def _move_custom(self, step: int) -> None:
        i = self._selected_custom()
        if i is not None and 0 <= i + step < len(self.custom):
            self.custom[i], self.custom[i + step] = self.custom[i + step], self.custom[i]
            self.custom_dirty = True
            self._fill_custom(i + step)

    def _save_custom(self) -> None:
        base = self.app.cfg.base_dir
        for c in self.custom:
            path = base / c["tags_file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            text = c["text"]
            path.write_text(text if not text or text.endswith("\n") else text + "\n", encoding="utf-8")
        save_custom_folders(self.app.config_path, self.custom)

    # --- построение полей ---

    def _tab(self, book: ttk.Notebook, title: str) -> ttk.Frame:
        tab = ttk.Frame(book, padding=10)
        tab.columnconfigure(1, weight=1)
        book.add(tab, text=title)
        return tab

    def _row(self, tab) -> int:
        return tab.grid_size()[1]

    def _section(self, tab, text: str) -> None:
        ttk.Label(tab, text=text, font=("Segoe UI", 10, "bold")).grid(
            row=self._row(tab), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 3))

    def _hint(self, tab, text: str | None) -> None:
        if text:
            ttk.Label(tab, text=text, wraplength=460, **HINT).grid(
                row=self._row(tab), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

    def _entry(self, tab, label, section, key, value, secret=False):
        row = self._row(tab)
        var = tk.StringVar(value=str(value))
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", **PAD)
        ttk.Entry(tab, textvariable=var, width=36, show="•" if secret else "").grid(
            row=row, column=1, sticky="ew", **PAD)
        self.fields.append((section, key, var, "str", value))

    def _check(self, tab, label, section, key, value):
        var = tk.BooleanVar(value=bool(value))
        ttk.Checkbutton(tab, text=label, variable=var).grid(
            row=self._row(tab), column=0, columnspan=2, sticky="w", **PAD)
        self.fields.append((section, key, var, "bool", bool(value)))

    def _choice(self, tab, label, section, key, value, options: dict[str, str]):
        row = self._row(tab)
        var = tk.StringVar(value=options.get(value, value))
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", **PAD)
        ttk.Combobox(tab, textvariable=var, values=list(options.values()), state="readonly", width=34).grid(
            row=row, column=1, sticky="w", **PAD)
        reverse = {v: k for k, v in options.items()}
        self.fields.append((section, key, var, ("choice", reverse), value))

    def _number(self, tab, label, section, key, value, low, high, step, hint=None, kind=float):
        row = self._row(tab)
        var = tk.StringVar(value=f"{value:g}" if isinstance(value, float) else str(value))
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", **PAD)
        ttk.Spinbox(tab, textvariable=var, from_=low, to=high, increment=step, width=8).grid(
            row=row, column=1, sticky="w", **PAD)
        self.fields.append((section, key, var, ("number", kind, low, high, label), value))
        self._hint(tab, hint)

    # --- сохранение ---

    def save(self) -> None:
        changes = []
        for section, key, var, kind, old in self.fields:
            raw = var.get()
            if kind == "str":
                new = raw.strip()
            elif kind == "bool":
                new = bool(raw)
            elif kind[0] == "choice":
                new = kind[1].get(raw, raw)
            else:
                _, cast, low, high, label = kind
                try:
                    new = cast(float(str(raw).replace(",", ".")))
                except ValueError:
                    messagebox.showerror("Настройки", f"«{label}»: нужно число.", parent=self.win)
                    return
                if not low <= new <= high:
                    messagebox.showerror("Настройки", f"«{label}»: от {low:g} до {high:g}.", parent=self.win)
                    return
            if kind == "str" and section == "folders" and not new:
                messagebox.showerror("Настройки", "Название папки не может быть пустым.", parent=self.win)
                return
            if new != old:
                changes.append((section, key, new))
        builtin = {v.lower() for sec, key, var, _, _ in self.fields if sec == "folders" for v in [var.get().strip()]}
        clash = [c["name"] for c in self.custom if c["name"].lower() in builtin]
        if clash:
            messagebox.showerror("Настройки", f"Своя папка «{clash[0]}» называется так же, как встроенная.",
                                 parent=self.win)
            return
        for section, key, value in changes:
            set_setting(self.app.config_path, section, key, toml_value(value))
        if self.custom_dirty:
            self._save_custom()
        self.win.destroy()
        if changes or self.custom_dirty:
            if not self.app.reload_config():
                return
            self.app.log(f"Настройки сохранены (изменено: {len(changes) + bool(self.custom_dirty)}).")
        if self.custom_dirty and messagebox.askyesno(
                "Настройки", "Свои папки сохранены.\n\nПересчитать уже разобранные файлы по новым правилам сейчас? "
                "Это быстро: без нейросети и без скачивания.", parent=self.app.root):
            self.app.do_recategorize()


def library_tags(db_path: Path) -> list[tuple[str, int]]:
    """Теги, которые уже встречаются в вашей базе, с количеством файлов."""
    if not db_path.exists():
        return []
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT tag, COUNT(DISTINCT file_id) AS n FROM tags GROUP BY tag ORDER BY n DESC").fetchall()
    finally:
        conn.close()


def model_tags(model: str) -> list[str]:
    """Все теги, которые знает нейросеть (если она уже скачана)."""
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(model, "selected_tags.csv")
    except Exception:
        return []
    if not isinstance(path, str):
        return []
    with open(path, encoding="utf-8") as f:
        return [r["name"] for r in csv.DictReader(f)]


class CategoriesWindow:
    """Категории артов по приоритету и их списки тегов, с поиском по известным тегам."""

    def __init__(self, app):
        self.app = app
        cfg: Config = app.cfg
        self.base = cfg.base_dir
        raw = tomllib.loads(app.config_path.read_text(encoding="utf-8"))
        cats = raw.get("categories", {})
        self.items = []
        for c in cats.get("list", []):
            path = self.base / c["tags_file"]
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            self.items.append({"name": c["name"], "file": c["tags_file"], "text": text})
        self.current: int | None = None
        self.known: list[tuple[str, str]] = []  # (тег, подпись)

        self.win = _window(app, "Категории и теги")
        self.win.minsize(820, 520)
        body = ttk.Frame(self.win, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text="Категории (сверху — главнее)", padding=8)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        self.listbox = tk.Listbox(left, height=14, width=28, exportselection=False)
        self.listbox.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._select())
        for i, (text, cmd) in enumerate((("Добавить", self.add), ("Переименовать", self.rename),
                                         ("Удалить", self.remove))):
            ttk.Button(left, text=text, command=cmd).grid(row=1, column=i, sticky="ew", pady=(6, 0))
        ttk.Button(left, text="▲ Выше", command=lambda: self.move(-1)).grid(row=2, column=0, sticky="ew", pady=4)
        ttk.Button(left, text="▼ Ниже", command=lambda: self.move(1)).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(left, text="Папка для остальных артов:").grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.default_var = tk.StringVar(value=cats.get("default", "Общее"))
        ttk.Entry(left, textvariable=self.default_var).grid(row=4, column=0, columnspan=3, sticky="ew")
        ttk.Label(left, text="Арт попадает в первую подходящую\nкатегорию, остальные подходящие\nзаписываются в теги.",
                  **HINT).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.title_var = tk.StringVar(value="Выберите категорию слева")
        ttk.Label(right, textvariable=self.title_var, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.text = tk.Text(right, height=12, wrap="none", undo=True)
        self.text.grid(row=1, column=0, sticky="nsew", pady=4)
        ttk.Label(right, text="Один тег на строку, # — комментарий. Подходят обычные теги (scenery), персонажи "
                  "(saber_(fate)) и вселенные — то, что у персонажа в скобках (fate, arknights).",
                  wraplength=520, **HINT).grid(row=2, column=0, sticky="w")

        search = ttk.LabelFrame(right, text="Найти тег", padding=6)
        search.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        search.columnconfigure(0, weight=1)
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *a: self._search())
        ttk.Entry(search, textvariable=self.query).grid(row=0, column=0, sticky="ew")
        ttk.Button(search, text="Добавить в категорию", command=self.add_found).grid(row=0, column=1, padx=(6, 0))
        self.found = tk.Listbox(search, height=6)
        self.found.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.found.bind("<Double-Button-1>", lambda e: self.add_found())
        self.search_hint = tk.StringVar(value="Загружаю список тегов…")
        ttk.Label(search, textvariable=self.search_hint, **HINT).grid(row=2, column=0, columnspan=2, sticky="w")

        bottom = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Открыть папку с файлами тегов",
                   command=lambda: open_path(self.base / "rules")).pack(side="left")
        ttk.Button(bottom, text="Сохранить", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Отмена", command=self.win.destroy).pack(side="right", padx=8)

        self._fill()
        if self.items:
            self.listbox.selection_set(0)
            self._select()
        threading.Thread(target=self._load_known, args=(cfg,), daemon=True).start()

    # --- список категорий ---

    def _fill(self) -> None:
        self.listbox.delete(0, "end")
        for i, item in enumerate(self.items, 1):
            self.listbox.insert("end", f"{i}. {item['name']}")

    def _store(self) -> None:
        if self.current is not None and self.current < len(self.items):
            self.items[self.current]["text"] = self.text.get("1.0", "end-1c")

    def _select(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        self._store()
        self.current = sel[0]
        item = self.items[self.current]
        self.title_var.set(f"Теги категории «{item['name']}»")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", item["text"])

    def _reselect(self, index: int) -> None:
        self._fill()
        if self.items:
            index = max(0, min(index, len(self.items) - 1))
            self.listbox.selection_set(index)
            self.current = None
            self._select()
        else:
            self.current = None
            self.title_var.set("Категорий нет: все арты пойдут в папку для остальных")
            self.text.delete("1.0", "end")

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Название категории (так будет называться папка):",
                                      initialvalue=initial, parent=self.win)
        name = (name or "").strip()
        if not name:
            return None
        if any(i["name"].lower() == name.lower() for i in self.items if i["name"] != initial):
            messagebox.showerror("Категории", f"Категория «{name}» уже есть.", parent=self.win)
            return None
        return name

    def add(self) -> None:
        name = self._ask_name("Новая категория")
        if not name:
            return
        self._store()
        used = {i["file"] for i in self.items}
        stem, n = safe_file_name(name), 1
        file = f"rules/{stem}.txt"
        while file in used or (self.base / file).exists():
            n += 1
            file = f"rules/{stem} {n}.txt"
        self.items.append({"name": name, "file": file, "text": f"# Теги категории «{name}», по одному на строку\n"})
        self._reselect(len(self.items) - 1)

    def rename(self) -> None:
        if self.current is None:
            return
        name = self._ask_name("Переименовать категорию", self.items[self.current]["name"])
        if name:
            self.items[self.current]["name"] = name
            self._reselect(self.current)

    def remove(self) -> None:
        if self.current is None:
            return
        name = self.items[self.current]["name"]
        if messagebox.askyesno("Категории", f"Удалить категорию «{name}»?\n\nФайл с её тегами останется на диске, "
                               "уже разложенные арты не тронутся.", parent=self.win):
            del self.items[self.current]
            self._reselect(self.current)

    def move(self, step: int) -> None:
        if self.current is None:
            return
        i, j = self.current, self.current + step
        if 0 <= j < len(self.items):
            self._store()
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self.current = None
            self._fill()
            self.listbox.selection_set(j)
            self._select()

    # --- поиск тегов ---

    def _load_known(self, cfg: Config) -> None:
        try:
            mine = library_tags(cfg.database)
        except Exception:
            mine = []
        seen = {t for t, _ in mine}
        known = [(t, f"{t}  — в вашей библиотеке: {n}") for t, n in mine]
        known += [(t, t) for t in model_tags(cfg.tagger_model) if t not in seen]
        self.known = known
        hint = (f"Тегов в вашей библиотеке: {len(mine)}, всего известно: {len(known)}. Двойной клик — добавить."
                if known else "Список тегов появится после первого разбора артов нейросетью.")
        try:
            self.win.after(0, lambda: (self.search_hint.set(hint), self._search()))
        except tk.TclError:  # окно уже закрыли
            pass

    def _search(self) -> None:
        q = normalize_tag(self.query.get().strip())
        self.found.delete(0, "end")
        if not q:
            return
        for tag, label in [k for k in self.known if q in k[0]][:200]:
            self.found.insert("end", label)

    def add_found(self) -> None:
        sel = self.found.curselection()
        if not sel or self.current is None:
            return
        tag = self.found.get(sel[0]).split("  — ")[0]
        lines = [normalize_tag(line) for line in self.text.get("1.0", "end-1c").splitlines()]
        if tag in lines:
            return
        content = self.text.get("1.0", "end-1c")
        self.text.insert("end", ("" if not content or content.endswith("\n") else "\n") + tag + "\n")
        self.text.see("end")

    # --- сохранение ---

    def save(self) -> None:
        self._store()
        default = self.default_var.get().strip()
        if not default:
            messagebox.showerror("Категории", "Укажите папку для остальных артов.", parent=self.win)
            return
        for item in self.items:
            path = self.base / item["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            text = item["text"] if item["text"].endswith("\n") or not item["text"] else item["text"] + "\n"
            path.write_text(text, encoding="utf-8")
        save_categories(self.app.config_path, default, [(i["name"], i["file"]) for i in self.items])
        self.win.destroy()
        if not self.app.reload_config():
            return
        self.app.log("Категории и теги сохранены.")
        if messagebox.askyesno("Категории", "Сохранено.\n\nПересчитать категории уже разобранных артов сейчас? "
                               "Это быстро: без нейросети и без скачивания файлов.", parent=self.app.root):
            self.app.do_recategorize()
