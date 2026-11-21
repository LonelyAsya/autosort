"""SQLite-база: файлы, теги, журнал переносов."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,        -- где файл лежит сейчас
    orig_path TEXT NOT NULL,          -- где лежал до сортировки
    month TEXT,
    size INTEGER,
    mtime REAL,
    kind TEXT,                        -- image | animation | video
    width INTEGER,
    height INTEGER,
    duration REAL,
    camera TEXT,
    taken_at TEXT,
    sha256 TEXT,
    phash TEXT,                       -- отпечатки кадров через запятую
    type TEXT,                        -- camera | art | meme | screenshot | review
    reason TEXT,
    category TEXT,
    author TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- hashed | analyzed | moved | trashed | error
    error TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_status ON files(status);

CREATE TABLE IF NOT EXISTS tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- general | character | universe | rating | category
    tag TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (file_id, kind, tag)
);
CREATE INDEX IF NOT EXISTS tags_kind ON tags(kind, tag);

CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY,
    batch INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    action TEXT NOT NULL,             -- move | trash
    src TEXT NOT NULL,
    dst TEXT,
    state TEXT NOT NULL,              -- pending | done | undone | failed
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dupe_ignored (
    group_key TEXT PRIMARY KEY        -- id файлов группы через запятую
);
"""

FILE_FIELDS = ("month", "size", "mtime", "kind", "width", "height", "duration", "camera", "taken_at",
               "sha256", "phash", "type", "reason", "category", "author", "source", "status", "error", "base_type")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "base_type" not in columns:  # база от версий до 2.1: тип без учёта своих папок
            with self.conn:
                self.conn.execute("ALTER TABLE files ADD COLUMN base_type TEXT")

    def close(self) -> None:
        self.conn.close()

    # --- файлы ---

    def get_by_path(self, path: Path | str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()

    def get(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def save_file(self, path: Path | str, tags: list[tuple[str, str, float]] | None = None, **fields) -> int:
        """Создаёт или обновляет запись о файле. tags=None — теги не трогаем."""
        fields = {k: v for k, v in fields.items() if k in FILE_FIELDS}
        fields["updated_at"] = now()
        row = self.get_by_path(path)
        with self.conn:
            if row is None:
                cols = ["path", "orig_path", *fields]
                self.conn.execute(
                    f"INSERT INTO files ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (str(path), str(path), *fields.values()),
                )
                file_id = self.conn.execute("SELECT id FROM files WHERE path = ?", (str(path),)).fetchone()[0]
            else:
                file_id = row["id"]
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE files SET {sets} WHERE id = ?", (*fields.values(), file_id))
            if tags is not None:
                self.conn.execute("DELETE FROM tags WHERE file_id = ?", (file_id,))
                self.conn.executemany(
                    "INSERT OR REPLACE INTO tags (file_id, kind, tag, score) VALUES (?, ?, ?, ?)",
                    [(file_id, kind, tag, score) for kind, tag, score in tags],
                )
        return file_id

    def set_location(self, file_id: int, path: Path | str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE files SET path = ?, status = ?, updated_at = ? WHERE id = ?",
                              (str(path), status, now(), file_id))

    def path_taken(self, path: Path | str, file_id: int | None = None) -> bool:
        """Путь уже записан за другим файлом базы (например, за дублем, удалённым в корзину)."""
        row = self.conn.execute("SELECT id FROM files WHERE path = ? AND id IS NOT ?",
                                (str(path), file_id)).fetchone()
        return row is not None

    def pending_moves(self) -> list[sqlite3.Row]:
        """Переносы, прерванные между перемещением файла и записью в базу."""
        return self.conn.execute(
            "SELECT * FROM moves WHERE action = 'move' AND state = 'pending' ORDER BY id").fetchall()

    def set_move_dst(self, move_id: int, dst: Path | str) -> None:
        with self.conn:
            self.conn.execute("UPDATE moves SET dst = ? WHERE id = ?", (str(dst), move_id))

    def set_status(self, file_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE files SET status = ?, updated_at = ? WHERE id = ?", (status, now(), file_id))

    def set_category(self, file_id: int, category: str, extra: list[str]) -> None:
        """Главная категория и запасные (как теги kind='category')."""
        with self.conn:
            self.conn.execute("UPDATE files SET category = ?, updated_at = ? WHERE id = ?",
                              (category, now(), file_id))
            self.conn.execute("DELETE FROM tags WHERE file_id = ? AND kind = 'category'", (file_id,))
            self.conn.executemany("INSERT INTO tags (file_id, kind, tag, score) VALUES (?, 'category', ?, 1.0)",
                                  [(file_id, c) for c in extra])

    def set_classification(self, file_id: int, type_: str, base_type: str, category: str | None,
                           extra: list[str]) -> None:
        """Тип, исходный тип (без своих папок), категория и запасные категории."""
        with self.conn:
            self.conn.execute("UPDATE files SET type = ?, base_type = ?, category = ?, updated_at = ? WHERE id = ?",
                              (type_, base_type, category, now(), file_id))
            self.conn.execute("DELETE FROM tags WHERE file_id = ? AND kind = 'category'", (file_id,))
            self.conn.executemany("INSERT INTO tags (file_id, kind, tag, score) VALUES (?, 'category', ?, 1.0)",
                                  [(file_id, c) for c in extra])

    def tags_of(self, file_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT kind, tag, score FROM tags WHERE file_id = ?", (file_id,)).fetchall()

    # --- журнал ---

    def new_batch(self) -> int:
        return (self.conn.execute("SELECT MAX(batch) FROM moves").fetchone()[0] or 0) + 1

    def journal(self, batch: int, file_id: int, action: str, src: Path | str, dst: Path | str | None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO moves (batch, file_id, action, src, dst, state, at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (batch, file_id, action, str(src), str(dst) if dst else None, now()),
            )
        return cur.lastrowid

    def journal_state(self, move_id: int, state: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE moves SET state = ? WHERE id = ?", (state, move_id))

    def batches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT batch, MIN(at) AS at, action, COUNT(*) AS n, SUM(state = 'done') AS done, "
            "SUM(state = 'undone') AS undone FROM moves GROUP BY batch, action ORDER BY batch"
        ).fetchall()

    def moves_of(self, batch: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM moves WHERE batch = ? ORDER BY id DESC", (batch,)).fetchall()

    def last_trash_batch(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(batch) FROM moves WHERE action = 'trash' AND state = 'done'").fetchone()
        return row[0]

    def last_move_batch(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(batch) FROM moves WHERE action = 'move' AND state = 'done'").fetchone()
        return row[0]

    # --- дубли ---

    def ignore_group(self, key: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO dupe_ignored (group_key) VALUES (?)", (key,))

    def ignored_groups(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT group_key FROM dupe_ignored")}
