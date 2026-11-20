"""Поиск точных и похожих дублей по хэшам и отпечаткам из базы."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .db import Database

ACTIVE = ("hashed", "analyzed", "moved")


@dataclass
class Group:
    key: str
    ids: list[int]
    keep: int
    exact: bool


def _popcount(x: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    return np.unpackbits(x[..., None].view(np.uint8), axis=-1).sum(axis=-1)


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, a: int) -> int:
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _similar_pairs(ids: list[int], hashes: np.ndarray, threshold: float):
    """Пары, у которых среднее расстояние Хэмминга по кадрам ≤ threshold."""
    for i in range(len(ids) - 1):
        d = _popcount(hashes[i + 1:] ^ hashes[i])
        if d.ndim > 1:
            d = d.mean(axis=1)
        for j in np.nonzero(d <= threshold)[0]:
            yield ids[i], ids[i + 1 + int(j)]


def keep_choice(rows: list) -> int:
    """Какой файл оставить: больше разрешение, затем больше вес, затем старше."""
    def key(r):
        pixels = (r["width"] or 0) * (r["height"] or 0)
        date = r["taken_at"] or "9999"
        return (-pixels, -(r["size"] or 0), date, r["mtime"] or 0)
    return min(rows, key=key)["id"]


def _inside(path: str, roots: list[Path]) -> bool:
    p = Path(path)
    return any(p == r or r in p.parents for r in roots)


def find_groups(db: Database, threshold: int, roots: list[Path] | None = None) -> list[Group]:
    """roots — искать только среди файлов в этих папках (None — среди всех известных базе)."""
    rows = db.conn.execute(
        f"SELECT * FROM files WHERE sha256 IS NOT NULL AND status IN ({','.join('?' * len(ACTIVE))})", ACTIVE
    ).fetchall()
    if roots is not None:
        resolved = [Path(r).resolve() for r in roots]
        rows = [r for r in rows if _inside(str(Path(r["path"]).resolve()), resolved)]
    rows = [r for r in rows if Path(r["path"]).exists()]
    by_id = {r["id"]: r for r in rows}
    uf = _UnionFind()
    exact_pairs: set[frozenset] = set()

    by_sha: dict[str, list[int]] = {}
    for r in rows:
        by_sha.setdefault(r["sha256"], []).append(r["id"])
    for ids in by_sha.values():
        for other in ids[1:]:
            uf.union(ids[0], other)
            exact_pairs.add(frozenset((ids[0], other)))

    # Похожие сравниваем отдельно: картинки с картинками, видео и анимации —
    # с теми, у кого столько же кадров. Одинаковые по sha уже склеены.
    buckets: dict[tuple[str, int], list] = {}
    seen_sha: set[str] = set()
    for r in rows:
        if not r["phash"] or r["sha256"] in seen_sha:
            continue
        seen_sha.add(r["sha256"])
        frames = r["phash"].split(",")
        group = "image" if r["kind"] == "image" else "moving"
        buckets.setdefault((group, len(frames)), []).append((r["id"], [int(h, 16) for h in frames]))
    for items in buckets.values():
        if len(items) < 2:
            continue
        ids = [i for i, _ in items]
        hashes = np.array([h for _, h in items], dtype=np.uint64)
        if hashes.shape[1] == 1:
            hashes = hashes[:, 0]
        for a, b in _similar_pairs(ids, hashes, threshold):
            uf.union(a, b)

    groups: dict[int, list[int]] = {}
    for r in rows:
        groups.setdefault(uf.find(r["id"]), []).append(r["id"])

    ignored = db.ignored_groups()
    result = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        ids.sort()
        key = ",".join(map(str, ids))
        if key in ignored:
            continue
        exact = len({by_id[i]["sha256"] for i in ids}) == 1
        result.append(Group(key, ids, keep_choice([by_id[i] for i in ids]), exact))
    result.sort(key=lambda g: (not g.exact, -len(g.ids)))
    return result


def reclaimable_bytes(db: Database, groups: list[Group]) -> int:
    total = 0
    for g in groups:
        for i in g.ids:
            if i != g.keep:
                total += db.get(i)["size"] or 0
    return total


def trash_files(db: Database, ids: list[int], log=print) -> int:
    """Отправляет файлы в корзину (не удаляет навсегда) и пишет это в журнал."""
    from send2trash import send2trash

    batch = db.new_batch()
    count = 0
    for i in ids:
        row = db.get(i)
        path = Path(row["path"])
        move_id = db.journal(batch, i, "trash", path, None)
        try:
            send2trash(str(path))
        except Exception as e:
            log(f"Не удалось удалить {path}: {e}")
            db.journal_state(move_id, "failed")
            continue
        db.set_status(i, "trashed")
        db.journal_state(move_id, "done")
        count += 1
    return count
