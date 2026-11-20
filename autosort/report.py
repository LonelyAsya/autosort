"""Текстовый отчёт report.txt."""
from __future__ import annotations

from collections import Counter

from .config import Config
from .db import Database
from .dupes import find_groups, reclaimable_bytes

TYPE_NAMES = ("camera", "art", "meme", "screenshot", "review")


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "0%"


def build_report(cfg: Config, db: Database, top: int = 30) -> str:
    conn = db.conn
    rows = conn.execute("SELECT id, type, category, author FROM files WHERE status IN ('analyzed', 'moved')").fetchall()
    total = len(rows)
    arts = [r for r in rows if r["type"] == "art"]
    art_ids = {r["id"] for r in arts}
    n_art = len(arts)

    lines = [f"Отчёт autosort", "", f"Всего файлов: {total}"]
    types = Counter(r["type"] for r in rows)
    for t in TYPE_NAMES:
        lines.append(f"  {cfg.folders[t]}: {types.get(t, 0)} ({_pct(types.get(t, 0), total)})")
    custom = Counter(r["category"] for r in rows if r["type"] == "custom")
    for name, n in custom.most_common():
        lines.append(f"  {name} (своя папка): {n} ({_pct(n, total)})")

    def section(title: str, counter: Counter) -> None:
        lines.extend(["", f"{title} (от числа артов: {n_art})"])
        if not counter:
            lines.append("  —")
        for name, n in counter.most_common(top):
            lines.append(f"  {name}: {n} ({_pct(n, n_art)})")

    section("Типы контента", Counter(r["category"] for r in arts))

    def tag_counter(kind: str) -> Counter:
        c = Counter()
        for r in conn.execute("SELECT file_id, tag FROM tags WHERE kind = ?", (kind,)):
            if r["file_id"] in art_ids:
                c[r["tag"]] += 1
        return c

    section("Топ тегов", tag_counter("general"))
    section("Топ персонажей", tag_counter("character"))
    section("Топ вселенных", tag_counter("universe"))
    authors = Counter(r["author"] or "неизвестен" for r in arts)
    section("Топ авторов", authors)

    groups = find_groups(db, cfg.phash_distance)
    extra = sum(len(g.ids) - 1 for g in groups)
    gb = reclaimable_bytes(db, groups) / 1024**3
    lines.extend(["", "Дубли", f"  Групп: {len(groups)}, лишних файлов: {extra}, можно освободить: {gb:.2f} ГБ"])
    return "\n".join(lines) + "\n"
