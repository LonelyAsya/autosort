"""Окно разбора дублей в браузере (работает только на этом компьютере)."""
from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path

from .config import Config
from .db import Database
from .dupes import Group, find_groups, trash_files
from .hashing import thumb_path


class ReviewState:
    def __init__(self, cfg: Config, db: Database, log=print, roots: list[Path] | None = None):
        self.cfg = cfg
        self.db = db
        self.log = log
        self.roots = roots
        self.batch: int | None = None  # все удаления одного разбора — одна запись в журнале
        self.groups: dict[str, Group] = {}
        self.refresh()

    def refresh(self) -> None:
        self.groups = {g.key: g for g in find_groups(self.db, self.cfg.phash_distance, self.roots)}

    def to_json(self) -> list[dict]:
        out = []
        for g in self.groups.values():
            items = []
            for i in g.ids:
                r = self.db.get(i)
                date = r["taken_at"] or (datetime.fromtimestamp(r["mtime"]).isoformat(timespec="seconds")
                                         if r["mtime"] else "")
                items.append(dict(id=i, name=Path(r["path"]).name, path=r["path"], width=r["width"],
                                  height=r["height"], size=r["size"], date=date, kind=r["kind"],
                                  similarity=(g.similarity or {}).get(i)))
            out.append(dict(key=g.key, keep=g.keep, exact=g.exact, items=items))
        return out

    def resolve(self, key: str, keep: list[int]) -> int:
        g = self.groups.get(key)
        keep = [i for i in keep if g is not None and i in g.ids]
        if g is None or not keep:  # хотя бы один файл из группы всегда остаётся
            return 0
        del self.groups[key]
        if self.batch is None:
            self.batch = self.db.new_batch()
        return trash_files(self.db, [i for i in g.ids if i not in keep], self.log, self.batch)

    def ignore(self, key: str) -> None:
        if self.groups.pop(key, None):
            self.db.ignore_group(key)

    def resolve_all(self) -> int:
        total = 0
        for key in list(self.groups):
            total += self.resolve(key, [self.groups[key].keep])
        return total


def make_handler(state: ReviewState):
    page = resources.files("autosort").joinpath("review.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/groups":
                return self._json(state.to_json())
            if self.path.startswith("/thumb/") or self.path.startswith("/file/"):
                kind, _, raw_id = self.path.strip("/").partition("/")
                row = state.db.get(int(raw_id)) if raw_id.isdigit() else None
                if row is None:
                    return self._send(404, b"", "text/plain")
                if kind == "thumb":
                    path = thumb_path(state.cfg.cache_dir, row["sha256"])
                    ctype = "image/jpeg"
                else:
                    path = Path(row["path"])
                    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if not path.exists():
                    return self._send(404, b"", "text/plain")
                return self._send(200, path.read_bytes(), ctype)
            self._send(404, b"", "text/plain")

        def do_POST(self):
            # Простая защита от запросов с чужих сайтов: только JSON с нашей страницы.
            if self.headers.get("Content-Type") != "application/json":
                return self._send(415, b"", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/resolve":
                return self._json({"trashed": state.resolve(data["key"], [int(i) for i in data["keep"]])})
            if self.path == "/api/ignore":
                state.ignore(data["key"])
                return self._json({"ok": True})
            if self.path == "/api/resolve_all":
                return self._json({"trashed": state.resolve_all()})
            if self.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._send(404, b"", "text/plain")

    return Handler


def create_server(cfg: Config, db: Database, port: int = 8765, log=print,
                  roots: list[Path] | None = None) -> tuple[HTTPServer, str] | None:
    """Готовит сервер окна дублей. None — дублей нет. Если порт занят, берёт следующий.

    roots — в каких папках искать; по умолчанию источник и папка результата."""
    if roots is None:
        roots = [cfg.source, cfg.target]
    state = ReviewState(cfg, db, log, roots)
    if not state.groups:
        log("Дублей не найдено.")
        return None
    for p in range(port, port + 20):
        try:
            server = HTTPServer(("127.0.0.1", p), make_handler(state))
            break
        except OSError:
            continue
    else:
        raise OSError(f"Порты {port}–{port + 19} заняты")
    url = f"http://127.0.0.1:{server.server_port}/"
    log(f"Групп дублей: {len(state.groups)}. Окно разбора: {url}")
    return server, url


def serve(cfg: Config, db: Database, port: int = 8765, open_browser: bool = True, log=print,
          roots: list[Path] | None = None) -> None:
    created = create_server(cfg, db, port, log, roots)
    if created is None:
        return
    server, url = created
    log("Ctrl+C — выход")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
