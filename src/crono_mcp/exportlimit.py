"""Respect Cronometer's personal export limit: 10 data exports per day.

Two guards, persisted next to the food cache so they survive restarts and are
shared by every process (the file is re-read before each check and update):
  * after Cronometer answers an export with HTTP 429, no export is attempted for 24 hours
    (retrying may extend the block, and exports made on the website count too);
  * this client stops on its own after 10 exports in a rolling 24 hours.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from crono_mcp.foodcache import default_path

DAILY_LIMIT = 10
WINDOW_SECONDS = 24 * 3600


class ExportLimit:
    def __init__(self, path: Path | None = None, *, persist: bool = True, now=time.time):
        self.path = path or default_path().parent / "export-limit.json"
        self._persist = persist
        self._now = now
        self._state: dict | None = None

    def _load(self) -> dict:
        if self._state is None or self._persist:  # persisted: always re-read, another process may have written
            self._state = {"blocked_until": 0.0, "recent": []}
            if self._persist:
                try:
                    data = json.loads(self.path.read_text())
                    self._state["blocked_until"] = float(data.get("blocked_until", 0))
                    self._state["recent"] = [float(t) for t in data.get("recent", [])]
                except (OSError, ValueError, TypeError):
                    pass
        return self._state

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def blocked_reason(self) -> str | None:
        """Why an export must not be attempted now, or None if it may."""
        s, now = self._load(), self._now()
        if s["blocked_until"] > now:
            until = datetime.fromtimestamp(s["blocked_until"]).strftime("%Y-%m-%d %H:%M")
            return f"Cronometer's daily export limit was hit; exports paused until {until}."
        recent = [t for t in s["recent"] if now - t < WINDOW_SECONDS]
        if len(recent) >= DAILY_LIMIT:
            until = datetime.fromtimestamp(min(recent) + WINDOW_SECONDS).strftime("%Y-%m-%d %H:%M")
            return f"Used {DAILY_LIMIT} exports in the last 24 hours (Cronometer's limit); next one at {until}."
        return None

    def status(self) -> dict:
        """Budget snapshot for tool results, so the model can see how scarce exports are."""
        s, now = self._load(), self._now()
        used = len([t for t in s["recent"] if now - t < WINDOW_SECONDS])
        out = {"exports_used_last_24h": used, "exports_remaining": max(0, DAILY_LIMIT - used),
               "daily_limit": DAILY_LIMIT}
        if s["blocked_until"] > now:
            out["exports_remaining"] = 0
            out["paused_until"] = datetime.fromtimestamp(s["blocked_until"]).isoformat(timespec="minutes")
        return out

    def record_success(self) -> None:
        s, now = self._load(), self._now()
        s["recent"] = [t for t in s["recent"] if now - t < WINDOW_SECONDS] + [now]
        self._save()

    def record_rate_limited(self) -> None:
        s = self._load()
        s["blocked_until"] = self._now() + WINDOW_SECONDS
        self._save()
