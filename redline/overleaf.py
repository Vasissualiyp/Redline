"""The Overleaf account behind `ols`: list its projects, create an empty one.

`ols` keeps its login (a pickle of the session cookie and a CSRF token) in
~/.olauth; this reads the same file, so no second login is needed. `ols` itself
cannot create projects, hence the small HTTP client here.
"""
from __future__ import annotations

import html
import json
import pickle
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .model import RedlineError

BASE = "https://www.overleaf.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


def _cookie() -> str:
    p = Path.home() / ".olauth"
    if not p.exists():
        raise RedlineError("no Overleaf login (~/.olauth); run `ols login`")
    try:
        store = pickle.loads(p.read_bytes())
        return "; ".join(f"{k}={v}" for k, v in dict(store["cookie"]).items())
    except Exception as e:
        raise RedlineError(f"cannot read {p}: {e}")


def _request(path: str, data: dict | None = None, csrf: str = "") -> bytes:
    headers = {"User-Agent": UA, "Cookie": _cookie()}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers.update({"Content-Type": "application/json", "X-Csrf-Token": csrf})
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RedlineError(f"Overleaf answered {e.code} for {path}"
                           + (" (session expired? `ols login`)" if e.code in (401, 403) else ""))
    except OSError as e:
        raise RedlineError(f"cannot reach Overleaf: {e}")


def _meta(page: str, name: str) -> str | None:
    m = re.search(rf'<meta\s+name="{re.escape(name)}"[^>]*?\scontent="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


def projects() -> list[dict]:
    """The account's live projects, newest first: [{id, name, updated}]."""
    page = _request("/project").decode("utf-8", "replace")
    blob = _meta(page, "ol-prefetchedProjectsBlob") or _meta(page, "ol-projects")
    if not blob:
        raise RedlineError("cannot read the project list from Overleaf (session expired? `ols login`)")
    data = json.loads(blob)
    rows = data["projects"] if isinstance(data, dict) else data
    out = [{"id": p.get("id") or p.get("_id"), "name": p["name"], "updated": p.get("lastUpdated", "")}
           for p in rows if not p.get("archived") and not p.get("trashed")]
    return sorted(out, key=lambda p: p["updated"], reverse=True)


def pretty(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp[:16]


def create(name: str) -> str:
    """Create an empty Overleaf project; its id."""
    if any(p["name"] == name for p in projects()):
        raise RedlineError(f"Overleaf already has a project called {name!r}")
    page = _request("/project").decode("utf-8", "replace")
    csrf = _meta(page, "ol-csrfToken")
    if not csrf:
        raise RedlineError("no CSRF token from Overleaf (session expired? `ols login`)")
    res = json.loads(_request("/project/new", {"projectName": name, "template": "none"}, csrf))
    pid = res.get("project_id") or res.get("id")
    if not pid:
        raise RedlineError(f"Overleaf did not create the project: {res}")
    return pid
