"""Поиск автора: по имени файла и (по желанию) через SauceNAO."""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from pathlib import Path

from .config import AuthorPattern


def from_filename(name: str, patterns: list[AuthorPattern]) -> tuple[str | None, str | None]:
    """(автор, источник) по шаблонам из настроек."""
    for p in patterns:
        m = p.regex.search(name)
        if not m:
            continue
        groups = m.groupdict()
        author = groups.get("author")
        source = f"{p.name}:{groups['id']}" if groups.get("id") else p.name
        return author, source
    return None, None


class SauceNao:
    URL = "https://saucenao.com/search.php?output_type=2&numres=3&db=999&api_key="

    def __init__(self, api_key: str, min_similarity: float, delay: float):
        self.api_key = api_key
        self.min_similarity = min_similarity
        self.delay = delay
        self._last = 0.0

    def lookup(self, path: Path) -> tuple[str | None, str | None]:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"image\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.URL + self.api_key, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)

        for item in data.get("results") or []:
            if float(item["header"]["similarity"]) < self.min_similarity:
                continue
            d = item["data"]
            author = (d.get("member_name") or d.get("twitter_user_handle") or d.get("author_name")
                      or d.get("creator"))
            if isinstance(author, list):
                author = ", ".join(author)
            if author:
                urls = d.get("ext_urls") or []
                return str(author), urls[0] if urls else "saucenao"
        return None, None
