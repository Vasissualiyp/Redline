"""latexmk runner + log parser that maps errors back to changes."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .model import Document
from .ops import apply, decide, resolve
from .state import Project

_FILE_LINE = re.compile(r"^(?P<file>[^\s:][^:]*\.tex):(?P<line>\d+): (?P<msg>.*)$")
_BANG = re.compile(r"^! (?P<msg>.*)$")
_LNUM = re.compile(r"^l\.(?P<line>\d+) ?(?P<ctx>.*)$")
_UNDEF = re.compile(r"(Citation|Reference) [`'](?P<key>[^']+)' on page \S+ undefined"
                    r"(?: on input line (?P<line>\d+))?")


@dataclass
class TexError:
    line: int
    message: str
    context: str = ""
    file: str = ""
    item: str = ""        # change label, e.g. C12
    item_id: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CompileResult:
    ok: bool
    pdf: str
    seconds: float
    errors: list[TexError] = field(default_factory=list)
    undefined: list[dict] = field(default_factory=list)
    warnings: int = 0
    log_tail: str = ""
    isolated: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "pdf": self.pdf, "seconds": round(self.seconds, 1),
                "errors": [e.as_dict() for e in self.errors], "undefined": self.undefined,
                "warnings": self.warnings, "isolated": self.isolated,
                "log_tail": "" if self.ok else self.log_tail}


def parse_log(log: str, main_name: str) -> tuple[list[TexError], list[dict], int]:
    errors: list[TexError] = []
    lines = log.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _FILE_LINE.match(ln)
        b = _BANG.match(ln) if not m else None
        if m or b:
            err = TexError(line=int(m["line"]) if m else 0, message=(m or b)["msg"].strip(),
                           file=m["file"] if m else "")
            # the context line "l.123 ..." follows within a few lines
            for j in range(i + 1, min(i + 12, len(lines))):
                lm = _LNUM.match(lines[j])
                if lm:
                    err.line = err.line or int(lm["line"])
                    err.context = lm["ctx"].strip()
                    break
            if not err.file or Path(err.file).name == main_name:
                key = (err.line, err.message)
                if key not in {(e.line, e.message) for e in errors}:
                    errors.append(err)
        i += 1
    undefined = []
    seen = set()
    for m in _UNDEF.finditer(log):
        k = (m.group(1), m["key"])
        if k not in seen:
            seen.add(k)
            undefined.append({"kind": m.group(1).lower(), "key": m["key"],
                              "line": int(m["line"]) if m["line"] else 0})
    warnings = len(re.findall(r"^(?:LaTeX|Package \S+) Warning", log, re.M))
    return errors[:30], undefined, warnings


def map_errors(doc: Document, errors: list[TexError]) -> None:
    for e in errors:
        best = None
        for it in doc.items:
            if it.line_start <= e.line <= it.line_end:
                best = it
                break
            # TeX often reports an error a line or two after the macro closes
            if it.line_end < e.line <= it.line_end + 2:
                best = it
        if best is not None:
            e.item, e.item_id = best.label, best.id


def _latexmk(proj: Project, tex: Path, outdir: Path, timeout: int) -> tuple[int, float]:
    outdir.mkdir(parents=True, exist_ok=True)
    root = proj.cfg.root
    env = dict(os.environ)
    for var in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
        env[var] = f"{root}{os.pathsep}{env.get(var, '')}"
    cmd = ["latexmk", "-pdf", "-synctex=1", "-interaction=nonstopmode", "-file-line-error",
           f"-outdir={outdir}", str(tex)]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    return rc, time.time() - t0


def build_dir(proj: Project) -> Path:
    return proj.dir / "build"


def pdf_path(proj: Project) -> Path:
    return build_dir(proj) / (proj.main.stem + ".pdf")


def run(proj: Project, isolate: bool = False) -> CompileResult:
    if not shutil.which("latexmk"):
        return CompileResult(False, "", 0, [TexError(0, "latexmk not found on PATH")])
    proj.flush_editor()
    outdir = build_dir(proj)
    rc, secs = _latexmk(proj, proj.main, outdir, proj.cfg.compile_timeout)
    log_path = outdir / (proj.main.stem + ".log")
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    errors, undefined, warnings = parse_log(log, proj.main.name)
    if rc == -1:
        errors.insert(0, TexError(0, f"latexmk timed out after {proj.cfg.compile_timeout}s"))
    elif rc != 0 and not errors:
        errors.append(TexError(0, f"latexmk exited with {rc} (see log tail)"))
    doc = proj.doc()
    map_errors(doc, errors)
    res = CompileResult(ok=rc == 0 and not errors, pdf=str(pdf_path(proj)), seconds=secs,
                        errors=errors, undefined=undefined, warnings=warnings,
                        log_tail="\n".join(log.splitlines()[-40:]))
    if isolate and errors:
        res.isolated = isolate_errors(proj, doc, errors)
    return res


def isolate_errors(proj: Project, doc: Document, errors: list[TexError], limit: int = 3) -> list[dict]:
    """Recompile with each suspect change rejected to confirm it is the culprit."""
    out = []
    suspects = []
    for e in errors:
        if e.item and e.item not in suspects:
            suspects.append(e.item)
    iso = proj.dir / "isolate"
    for label in suspects[:limit]:
        it = doc.find(label)
        edits = resolve(doc, decide(doc, [it], "reject", honor_markers=False), explicit=True)
        tex = iso / proj.main.name
        tex.parent.mkdir(parents=True, exist_ok=True)
        tex.write_text(apply(doc.src, edits))
        rc, _ = _latexmk(proj, tex, iso / "build", proj.cfg.compile_timeout)
        log_path = iso / "build" / (proj.main.stem + ".log")
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        errs, _, _ = parse_log(log, proj.main.name)
        out.append({"item": label, "id": it.id, "compiles_without_it": rc == 0 and not errs,
                    "remaining_errors": [f"l.{x.line}: {x.message}" for x in errs[:3]]})
    return out


def format_report(res: CompileResult) -> str:
    if res.ok:
        s = f"Compiled OK in {res.seconds:.0f}s -> {res.pdf}"
    else:
        s = f"Compile FAILED ({len(res.errors)} error(s), {res.seconds:.0f}s)"
    for e in res.errors:
        where = f"l.{e.line}" + (f" [{e.item} {e.item_id}]" if e.item else "")
        s += f"\n  {where}: {e.message}" + (f"  | {e.context}" if e.context else "")
    if res.undefined:
        s += "\n  undefined: " + ", ".join(f"{u['kind']} {u['key']}" for u in res.undefined[:15])
    for iso in res.isolated:
        verdict = "compiles without it" if iso["compiles_without_it"] else "still fails without it"
        s += f"\n  isolate {iso['item']}: {verdict}"
    return s
