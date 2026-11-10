"""Read-only Cronometer client.

Cronometer has no public API for individuals. This client uses the same
calls the web app makes when you click "Export Data":

  1. GWT-RPC ``authenticate``               -> your numeric user id
  2. GWT-RPC ``generateAuthorizationToken`` -> short-lived export token
  3. ``GET /export?generate=<type>``        -> CSV

READ-ONLY BY CONSTRUCTION: the only POST this module can send is one of the
two fixed RPC bodies in ``_RPC_BODIES``; everything else is a GET. There is
no code path that can modify your diary.

Protocol details adapted from cphoskins/cronometer-mcp (MIT, see
THIRD_PARTY_LICENSE_cronometer-mcp).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime

import httpx

from crono_mcp import auth

logger = logging.getLogger(__name__)
# httpx logs full URLs at INFO, which would include short-lived export tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE = "https://cronometer.com"
GWT_APP_URL = f"{BASE}/cronometer/app"
GWT_MODULE_BASE = f"{BASE}/cronometer/"
GWT_NOCACHE_JS_URL = f"{BASE}/cronometer/cronometer.nocache.js"
GWT_CACHE_JS_URL = f"{BASE}/cronometer/{{permutation}}.cache.js"
EXPORT_URL = f"{BASE}/export"

# Fallbacks only; discovered fresh from Cronometer's JS on first use.
FALLBACK_PERMUTATION = "CBC38FBB0A1527BD5E68722DD9DABD27"
FALLBACK_GWT_HEADER = "76FC4464E20E53D16663AC9A96A486B3"

EXPORT_TYPES = {
    "servings": "servings",
    "daily_summary": "dailySummary",
    "exercises": "exercises",
    "biometrics": "biometrics",
    "notes": "notes",
}

# The ONLY RPC calls this client may make. Both are reads.
_RPC_BODIES = {
    "authenticate": (
        "7|0|5|https://cronometer.com/cronometer/|{gwt_header}|"
        "com.cronometer.shared.rpc.CronometerService|"
        "authenticate|java.lang.Integer/3438268394|"
        "1|2|3|4|1|5|5|{tz_offset}|"
    ),
    "generateAuthorizationToken": (
        "7|0|8|https://cronometer.com/cronometer/|{gwt_header}|"
        "com.cronometer.shared.rpc.CronometerService|"
        "generateAuthorizationToken|java.lang.String/2004016611|"
        "I|com.cronometer.shared.user.AuthScope/2065601159|"
        "{nonce}|1|2|3|4|4|5|6|6|7|8|{user_id}|3600|7|2|"
    ),
}

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class AuthError(RuntimeError):
    """Session missing/expired. Fix: log into cronometer.com in Chrome, then refresh auth."""


class CronometerError(RuntimeError):
    pass


def _local_tz_offset_minutes() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


class CronometerClient:
    def __init__(self, cookies: dict[str, str] | None = None, *, persist: bool = True):
        self._persist = persist
        self._cookies: dict[str, str] | None = cookies
        self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                                  headers={"User-Agent": "crono-mcp-readonly/0.1"})
        self._permutation = FALLBACK_PERMUTATION
        self._gwt_header = FALLBACK_GWT_HEADER
        self._hashes_discovered = False
        self._user_id: str | None = None

    # ---------- cookies ----------
    def _ensure_cookies(self) -> dict[str, str]:
        if self._cookies is None:
            self._cookies = auth.load()
        if self._cookies is None:
            # Deliberately NOT reading Chrome here: each Chrome read can trigger a
            # macOS "Chrome Safe Storage" prompt. Only explicit auth/refresh does that.
            raise AuthError("No stored Cronometer session. Run `crono-mcp auth` (or the refresh tool) once.")
        return self._cookies

    def reload_from_browser(self, browser: str = "chrome") -> str:
        res = auth.extract_from_browser(browser)
        if not res.success:
            raise AuthError(res.message)
        self._cookies = res.cookies
        self._user_id = None
        if self._persist:
            auth.store(self._cookies)
        return res.message

    def _absorb_set_cookies(self, resp: httpx.Response) -> None:
        """Keep up with session-cookie rotation the server does on our requests."""
        changed = False
        for name, value in resp.cookies.items():
            if value and self._cookies is not None and self._cookies.get(name) != value:
                self._cookies[name] = value
                changed = True
        self._http.cookies.clear()  # we manage cookies ourselves
        if changed and self._persist and self._cookies:
            auth.store(self._cookies)

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._ensure_cookies().items())

    # ---------- GWT plumbing ----------
    def _discover_hashes(self) -> None:
        if self._hashes_discovered:
            return
        self._hashes_discovered = True
        try:
            r = self._http.get(GWT_NOCACHE_JS_URL)
            r.raise_for_status()
            m = re.search(r"='([A-F0-9]{32})'", r.text)
            if not m:
                logger.warning("GWT permutation not found; using fallback")
                return
            perm = m.group(1)
            r = self._http.get(GWT_CACHE_JS_URL.format(permutation=perm))
            r.raise_for_status()
            self._permutation = perm
            h = re.search(r"'app','([A-F0-9]{32})'", r.text)
            if h:
                self._gwt_header = h.group(1)
            else:
                logger.warning("GWT header not found; using fallback")
        except httpx.HTTPError as e:
            logger.warning("GWT hash discovery failed (%s); using fallbacks", type(e).__name__)

    def _rpc(self, method: str) -> str:
        if method not in _RPC_BODIES:  # belt and braces: no other RPCs, ever
            raise CronometerError(f"RPC {method!r} is not permitted (read-only client)")
        self._discover_hashes()
        cookies = self._ensure_cookies()
        body = _RPC_BODIES[method].format(
            gwt_header=self._gwt_header,
            tz_offset=_local_tz_offset_minutes(),
            nonce=cookies.get(auth.SESSION_COOKIE, ""),
            user_id=self._user_id or "",
        )
        r = self._http.post(
            GWT_APP_URL,
            content=body.encode(),
            headers={
                "content-type": "text/x-gwt-rpc; charset=UTF-8",
                "x-gwt-module-base": GWT_MODULE_BASE,
                "x-gwt-permutation": self._permutation,
                "cookie": self._cookie_header(),
            },
        )
        self._absorb_set_cookies(r)
        text = r.text
        if "NotLoggedInException" in text or r.status_code in (401, 403):
            raise AuthError("Cronometer session expired or invalid.")
        if r.status_code >= 400 or not text.startswith("//OK"):
            # Response may include the IncompatibleRemoteService text that indicates a Cronometer deploy.
            raise CronometerError(f"Cronometer RPC {method} failed (HTTP {r.status_code}): {text[:200]}")
        return text

    def _user(self) -> str:
        if self._user_id is None:
            m = re.search(r"OK\[(\d+),", self._rpc("authenticate"))
            if not m:
                raise CronometerError("Could not read user id from Cronometer authenticate response")
            self._user_id = m.group(1)
        return self._user_id

    def _export_token(self) -> str:
        self._user()
        text = self._rpc("generateAuthorizationToken")
        m = re.search(r'"([^"]+)"', text)
        if not m:
            raise CronometerError("Could not read export token from Cronometer response")
        return m.group(1)

    # ---------- public API ----------
    def check(self) -> str:
        """Validate the session; returns the Cronometer user id."""
        return self._user()

    def export_csv(self, export_type: str, start: date, end: date) -> str:
        if export_type not in EXPORT_TYPES:
            raise ValueError(f"export_type must be one of {sorted(EXPORT_TYPES)}")
        if end < start:
            raise ValueError("end date is before start date")
        token = self._export_token()
        r = self._http.get(
            EXPORT_URL,
            params={
                "nonce": token,
                "generate": EXPORT_TYPES[export_type],
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            headers={"cookie": self._cookie_header()},
        )
        self._absorb_set_cookies(r)
        if r.status_code in (401, 403):
            raise AuthError("Cronometer rejected the export request (session expired?).")
        if r.status_code >= 400:
            raise CronometerError(f"Cronometer export failed (HTTP {r.status_code})")
        text = r.text
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise AuthError("Cronometer returned a web page instead of CSV (session expired?).")
        return text

    def export_rows(self, export_type: str, start: date, end: date) -> list[dict[str, str]]:
        return parse_csv(self.export_csv(export_type, start, end))

    def close(self) -> None:
        self._http.close()


def parse_csv(text: str) -> list[dict[str, str]]:
    text = text.lstrip("﻿")
    if not text.strip():
        return []
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]
