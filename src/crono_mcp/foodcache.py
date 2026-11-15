"""On-disk cache of food nutrient profiles (getFood results).

Database foods (anything the user can't edit) are stored for up to 30 days.
The user's own recipes and custom foods are never written here: they are
fetched fresh on every request, so editing one can never leave a stale entry.
Day totals are checked against Cronometer's official numbers anyway (see
diary.py), which catches anything else.

Use one FoodCache per request: it reads the file when first used and writes it
back only if something was added, so a `logout` (which deletes the file) or
another process's writes are picked up by the next request.

The file holds nutrition data only (no session or account data). It lives in
the user cache directory with 0600 permissions and is removed by `crono-mcp logout`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from crono_mcp.gwt import Food

logger = logging.getLogger(__name__)

TTL_SECONDS = 30 * 24 * 3600
MAX_ENTRIES = 3000
VERSION = 3  # v3: Food.measures (display units)


def default_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / "Library" / "Caches"
    return root / "crono-mcp" / f"foods-v{VERSION}.json"


class FoodCache:
    def __init__(self, path: Path | None = None, *, now=time.time):
        self.path = path or default_path()
        self._now = now
        self._entries: dict[str, dict] | None = None
        self._dirty = False

    def _load(self) -> dict[str, dict]:
        if self._entries is None:
            try:
                data = json.loads(self.path.read_text())
                self._entries = data.get("foods", {}) if data.get("version") == VERSION else {}
            except (OSError, ValueError):
                self._entries = {}
        return self._entries

    def get(self, food_id: int) -> Food | None:
        e = self._load().get(str(food_id))
        if not e or self._now() - e.get("fetched", 0) > TTL_SECONDS:
            return None
        try:
            food = Food.from_json(e["food"])
        except (KeyError, TypeError, ValueError):
            return None
        return food

    def put(self, food: Food) -> None:
        if food.custom:
            return  # user's own recipes and custom foods are never cached
        self._load()[str(food.food_id)] = {"food": food.to_json(), "fetched": self._now()}
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        entries = self._load()
        now = self._now()
        entries = {k: e for k, e in entries.items() if now - e.get("fetched", 0) <= TTL_SECONDS}
        if len(entries) > MAX_ENTRIES:
            entries = dict(sorted(entries.items(), key=lambda kv: kv[1]["fetched"], reverse=True)[:MAX_ENTRIES])
        self._entries = entries
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".foods-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"version": VERSION, "foods": entries}, f, ensure_ascii=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                self._remove_old_versions()
            except BaseException:
                os.unlink(tmp)
                raise
            self._dirty = False
        except OSError as e:
            logger.warning("Could not write food cache (%s)", type(e).__name__)

    def _remove_old_versions(self) -> None:
        for old in self.path.parent.glob("foods-v*.json"):
            if old != self.path:
                old.unlink(missing_ok=True)

    def clear(self) -> None:
        self._entries = {}
        self._dirty = False
        self.path.unlink(missing_ok=True)
        if self.path.parent.exists():
            self._remove_old_versions()
