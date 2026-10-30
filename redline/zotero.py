"""Zotero access for the evidence library.

Reads and note/collection writes use Zotero's local API (127.0.0.1:23119; writes
need the key granted once with `zotero-mcp authorize-local`). Highlights go
through `zotero-cli`, which finds the exact sentence on the page (PyMuPDF,
hyphenation and quote styles tolerated). Zotero sync then carries notes and
highlights to every machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .model import RedlineError

API = os.environ.get("ZOTERO_LOCAL_URL", "http://127.0.0.1:23119")
LIB = "/api/users/0"


class ZoteroError(RedlineError):
    pass


def _local_key() -> str:
    if os.environ.get("ZOTERO_LOCAL_API_KEY"):
        return os.environ["ZOTERO_LOCAL_API_KEY"]
    cfg = Path.home() / ".config" / "zotero-mcp"
    try:
        key = json.loads((cfg / "config.json").read_text()).get("client_env", {}).get("ZOTERO_LOCAL_API_KEY")
        if key:
            return key
    except (OSError, ValueError):
        pass
    try:
        return (cfg / "local_api_key").read_text().strip()
    except OSError:
        return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", plain(s or "").lower())


def plain(tex: str) -> str:
    """A .bib field without LaTeX: `H$_2$ in T$_{vir}$` -> `H2 in Tvir`."""
    s = re.sub(r"\\[A-Za-z]+\*?", " ", tex)
    s = re.sub(r"[{}$^_\\~]", "", s)
    return " ".join(s.split())


def parse_bib(text: str) -> dict[str, dict[str, str]]:
    """{key: {field: value}} with balanced braces (fields may share a line)."""
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,", text):
        fields, i = {}, m.end()
        depth_end = len(text)
        while i < depth_end:
            f = re.compile(r"\s*(\w+)\s*=\s*").match(text, i)
            if not f:
                break
            i = f.end()
            if i < len(text) and text[i] in "{\"":
                close = "}" if text[i] == "{" else "\""
                depth, j = 0, i
                while j < len(text):
                    c = text[j]
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                    if (close == "}" and depth == 0 and c == "}") or (close == '"' and j > i and c == '"' and depth == 0):
                        break
                    j += 1
                val, i = text[i + 1:j], j + 1
            else:
                v = re.compile(r"[^,}\n]*").match(text, i)
                val, i = v.group(0).strip(), v.end()
            fields[f.group(1).lower()] = " ".join(val.split())
            c = re.compile(r"\s*,?").match(text, i)
            i = c.end()
            if i < len(text) and text[i] == "}":
                break
        out[m.group(1)] = fields
    return out


class Zotero:
    def __init__(self, bibfile: Path | None = None, timeout: int = 60):
        self.bibfile = bibfile
        self.timeout = timeout
        self._server_id = ""
        self._resolved: dict[str, tuple[str, str]] = {}
        self._bib: dict[str, dict[str, str]] | None = None

    # ---- transport --------------------------------------------------------------
    def available(self) -> bool:
        try:
            with urllib.request.urlopen(API + "/connector/ping", timeout=3) as r:
                return r.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _request(self, method: str, path: str, body=None, headers: dict | None = None):
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"Zotero-API-Version": "3", **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._server_id = self._server_id or r.headers.get("Zotero-Server-ID", "")
                raw = r.read()
                return (json.loads(raw) if raw.strip() else None), r.headers
        except urllib.error.HTTPError as e:
            raise ZoteroError(f"Zotero {method} {path}: HTTP {e.code} {e.read()[:300]!r}")
        except (OSError, urllib.error.URLError) as e:
            raise ZoteroError(f"Zotero is not reachable at {API} ({e}); start Zotero, or run "
                              "`redline evidence sync` later")

    def get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        return self._request("GET", LIB + path + q)[0]

    def get_all(self, path: str, **params) -> list:
        out, start = [], 0
        while True:
            page = self.get(path, start=start, limit=100, **params) or []
            out += page
            if len(page) < 100:
                return out
            start += 100

    def _write(self, method: str, path: str, body=None, version: int | None = None):
        key = _local_key()
        if not key:
            raise ZoteroError("no local Zotero API key: run `zotero-mcp authorize-local` once")
        if not self._server_id:
            self._request("GET", "/api/")
        h = {"Zotero-API-Key": key, "Zotero-Server-ID": self._server_id}
        if version is not None:
            h["If-Unmodified-Since-Version"] = str(version)
        return self._request(method, LIB + path, body, h)[0]

    def _cli(self, *args: str, stdin: str | None = None) -> dict:
        env = dict(os.environ, ZOTERO_LOCAL="true")
        env.pop("PYTHONPATH", None)
        try:
            r = subprocess.run(["zotero-cli", "--json", *args], input=stdin, capture_output=True,
                               text=True, env=env, timeout=self.timeout * 3)
        except FileNotFoundError:
            raise ZoteroError("zotero-cli is not on PATH (it comes with zotero-mcp-server)")
        except subprocess.TimeoutExpired:
            raise ZoteroError(f"zotero-cli {args[0]} timed out")
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise ZoteroError(f"zotero-cli {' '.join(args[:2])}: {(r.stdout + r.stderr).strip()[-400:]}")
        if not out.get("ok"):
            raise ZoteroError(f"zotero-cli {' '.join(args[:2])}: {out.get('error') or out}")
        return out.get("data") or {}

    # ---- finding the source PDF --------------------------------------------------
    def _bib_entries(self) -> dict[str, dict[str, str]]:
        if self._bib is None:
            self._bib = parse_bib(self.bibfile.read_text(errors="replace")) \
                if self.bibfile and self.bibfile.exists() else {}
        return self._bib

    def resolve(self, bibkey: str, item_key: str = "") -> tuple[str, str]:
        """(item key, PDF attachment key) for a bib key."""
        if bibkey in self._resolved and not item_key:
            return self._resolved[bibkey]
        if not item_key:
            item_key = self._find_item(bibkey)
        att = self._pdf_attachment(item_key)
        self._resolved[bibkey] = (item_key, att)
        return item_key, att

    def _find_item(self, bibkey: str) -> str:
        hits = self._cli("search", "--mode", "citekey", bibkey, "--detail", "keys_only").get("items") or []
        if hits:
            return hits[0]["key"]
        fields = self._bib_entries().get(bibkey, {})
        title = plain(fields.get("title", ""))
        tries = [(fields.get("doi", ""), "everything", False), (fields.get("eprint", ""), "everything", False),
                 (" ".join(title.split()[:8]), "titleCreatorYear", True)]
        for q, mode, by_title in tries:
            if not q:
                continue
            items = self._cli("search", q, "--qmode", mode, "--limit", "10", "--detail", "summary").get("items") or []
            items = [it for it in items if it.get("itemType") not in ("attachment", "note", "annotation")]
            for it in items:
                if not by_title or _norm(it.get("title", "")) == _norm(title):
                    return it["key"]
            if by_title:  # titles differ in punctuation or LaTeX: closest one, if close enough
                import difflib
                best = max(items, default=None,
                           key=lambda it: difflib.SequenceMatcher(None, _norm(it.get("title", "")), _norm(title)).ratio())
                if best and difflib.SequenceMatcher(None, _norm(best.get("title", "")), _norm(title)).ratio() > 0.85:
                    return best["key"]
        raise ZoteroError(f"{bibkey}: not found in Zotero (no citekey, DOI, arXiv id or title match)")

    def _pdf_attachment(self, item_key: str) -> str:
        data = self.get(f"/items/{item_key}")
        if data.get("data", {}).get("contentType") == "application/pdf":
            return item_key  # already the attachment
        for ch in self.get(f"/items/{item_key}/children") or []:
            d = ch.get("data", {})
            if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
                return ch["key"]
        raise ZoteroError(f"item {item_key} has no PDF attachment")

    def pdf_path(self, att: str) -> Path:
        d = Path.home() / "Zotero" / "storage" / att
        pdfs = sorted(d.glob("*.pdf")) if d.is_dir() else []
        if pdfs:
            return pdfs[0]
        text = self._cli("path", att).get("text", "")
        m = re.search(r"Local path: `([^`]+)`", text)
        if m and Path(m.group(1)).exists():
            return Path(m.group(1))
        raise ZoteroError(f"the PDF of {att} is not on this machine")

    # ---- quotes and highlights -------------------------------------------------
    def check(self, att: str, quotes: list[tuple[int, str]]) -> list[dict]:
        """Dry-run highlights: one {ok, page_found, matched_text, error} per (page, text)."""
        lines = "\n".join(json.dumps({"page": p, "text": t, "comment": ""}) for p, t in quotes)
        res = self._cli("annotations", "batch", "--attachment-key", att, "--file", "-", "--dry-run", stdin=lines)
        out = sorted(res.get("results", []), key=lambda r: r.get("index", 0))
        return out + [{"ok": False, "error": "no result"}] * (len(quotes) - len(out))

    def find_page(self, item_or_att: str, text: str) -> int | None:
        words = text.split()
        probe = " ".join(words[:8]) if len(words) > 8 else text
        try:
            data = self._cli("read", item_or_att, "--find", probe)
        except ZoteroError:
            return None
        for key in ("pages", "matches", "results"):
            for m in data.get(key) or []:
                if isinstance(m, dict) and m.get("page"):
                    return int(m["page"])
        m = re.search(r"\bpage (\d+)", data.get("text", ""), re.I)
        return int(m.group(1)) if m else None

    def highlight(self, att: str, page: int, text: str, comment: str, tags: list[str], color: str) -> dict:
        """Create the highlight; return {key, page_index, rects} read back from Zotero."""
        self._cli("annotations", "create", "--attachment-key", att, "--page", str(page), "--text", text,
                  "--comment", comment, "--color", color, "--tags", ",".join(tags))
        tag = tags[-1]
        anns = [a for a in self.get(f"/items/{att}/children", tag=tag) or []
                if a.get("data", {}).get("itemType") == "annotation"]
        if not anns:
            raise ZoteroError(f"highlight created but not found again by tag {tag}")
        a = max(anns, key=lambda a: a.get("data", {}).get("dateAdded", ""))
        return {"key": a["key"], **self.position(a)}

    def position(self, ann: dict | str) -> dict:
        if isinstance(ann, str):
            ann = self.get(f"/items/{ann}")
        try:
            pos = json.loads(ann.get("data", {}).get("annotationPosition") or "{}")
        except ValueError:
            pos = {}
        return {"page_index": pos.get("pageIndex"), "rects": pos.get("rects") or []}

    def open_annotation(self, ann: str) -> None:
        self._cli("open", "--annotation", ann)

    # ---- notes and collections -------------------------------------------------
    def collection(self, path: str) -> str:
        """Key of the collection `A/B` (created when missing)."""
        parent = None
        for name in [p for p in path.split("/") if p]:
            cols = self.get_all("/collections")
            hit = next((c for c in cols if c["data"]["name"] == name
                        and (c["data"].get("parentCollection") or None) == parent), None)
            if hit is None:
                res = self._write("POST", "/collections",
                                  [{"name": name, **({"parentCollection": parent} if parent else {})}])
                hit = {"key": _created_key(res)}
            parent = hit["key"]
        return parent

    def save_note(self, html_text: str, tags: list[str], collection: str, key: str = "") -> str:
        if key:
            try:
                cur = self.get(f"/items/{key}")
                self._write("PATCH", f"/items/{key}", {"note": html_text, "tags": [{"tag": t} for t in tags],
                                                       "collections": [collection]},
                            version=cur.get("version"))
                return key
            except ZoteroError as e:
                if "404" not in str(e):
                    raise
        res = self._write("POST", "/items", [{"itemType": "note", "note": html_text,
                                              "tags": [{"tag": t} for t in tags], "collections": [collection]}])
        return _created_key(res)

    def notes_tagged(self, tag: str) -> list[dict]:
        return [n for n in self.get_all("/items", tag=tag, itemType="note")]

    def delete(self, key: str) -> None:
        cur = self.get(f"/items/{key}")
        self._write("DELETE", f"/items/{key}", version=cur.get("version"))


def _created_key(res) -> str:
    try:
        ok = res.get("successful") or {}
        if ok:
            return next(iter(ok.values()))["key"]
        return next(iter(res["success"].values()))
    except (AttributeError, KeyError, StopIteration, TypeError):
        raise ZoteroError(f"Zotero did not create the object: {res}")
