"""Opt-in on-disk cache for raw API responses.

Eventor publishes no rate limit and has no webhooks, so a polling tool should
avoid re-fetching the same data within a short period (for example when a
developer runs a dry run several times in a row). The cache stores the raw
response body keyed on the request, and expires entries after a TTL.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class ResponseCache:
    """Cache raw response bodies on disk with a time-to-live.

    Entries are keyed on a stable hash of the request (URL, sorted query
    parameters and a hash of the API key, because different keys can see
    different data). Keys are never written to disk in the clear.
    """

    def __init__(self, directory: str | Path, ttl_seconds: float = 3600) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = float(ttl_seconds)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(url: str, params: dict[str, str], namespace: str) -> str:
        payload = json.dumps([namespace, url, sorted(params.items())], separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.xml"

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            return None
        if age > self.ttl_seconds:
            return None
        return path.read_bytes()

    def put(self, key: str, body: bytes) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)

    def clear(self) -> int:
        """Delete every cached entry. Returns the number of files removed."""
        removed = 0
        for path in self.directory.glob("*.xml"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed
