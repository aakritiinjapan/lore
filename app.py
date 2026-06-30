"""Lore — FastAPI backend.

Architecture note: Cognee's embedded Kuzu graph is single-writer and uses spawned
DB-worker processes. A long-lived server that holds the graph fights that model
(lock conflicts, stale state, orphaned workers). So here the server NEVER holds the
graph: every graph op (ask / explain / build / connect-rebuild) runs in a short-lived
SUBPROCESS, serialized by a lock. scan / check_ref / memory are pure-Python (no graph)
and run in-process and instantly.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from build_graph import load_slice, first_line, tracked_paths, match_tracked
from regress import build_decisions, detect, removed_by_file, explain_with_cognee, is_strong, is_meaningful
from ingest_github import fetch_slice, fetch_multi_slice, save_slice, fetch_ref_diff
from cognee import search, SearchType
from cognee.tasks.storage import add_data_points
from cognee.infrastructure.databases.graph import get_graph_engine
from models import CodeUnit, Decision

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
GHSA_RE = re.compile(r":ghsa:`([^`]+)`")
PYTHON = sys.executable
_graph_lock = asyncio.Lock()
# lifecycle state mirrored from genuine Cognee writes (at_risk = improve, forgotten = forget)
LIFECYCLE = {"at_risk": set(), "forgotten": set()}

app = FastAPI(title="Lore")


class ScanReq(BaseModel):
    diff: str


class AskReq(BaseModel):
    query: str


class ConnectReq(BaseModel):
    repo: str
    path: Optional[str] = None          # single file (back-compat)
    paths: Optional[list] = None        # multiple files to track
    keyword: Optional[str] = None


class CheckRefReq(BaseModel):
    ref: str


class InspectReq(BaseModel):
    path: str = ""
    content: str


class ConfirmReq(BaseModel):
    sha: str


# ---------------------------------------------------------------- subprocess runner
async def _run_proc(script: str, payload: Optional[dict] = None, timeout: int = 240) -> dict:
    """Run a graph subprocess (serialized), parse its `LORE_RESULT {...}` line."""
    def run():
        return subprocess.run(
            [PYTHON, script], cwd=BASE,
            input=(json.dumps(payload) if payload is not None else None),
            capture_output=True, text=True, timeout=timeout,
        )
    async with _graph_lock:
        p = await asyncio.to_thread(run)
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("LORE_RESULT "):
            try:
                return json.loads(line[len("LORE_RESULT "):])
            except Exception:
                pass
    return {"error": ((p.stderr or "") + (p.stdout or ""))[-400:] or "no output"}


# ---------------------------------------------------------------- pure-python scan
def _dec(d):
    return {"sha": d["sha"], "date": d["date"], "message": d["message"], "ghsa": d["ghsa"]}


def _regression_for_file(file: str, fs: list) -> dict:
    """Summarize one file's findings into a regression record."""
    by_date = sorted(fs, key=lambda f: f["date"])
    origin, refinements = by_date[0], by_date[1:]
    strong, total = [], 0
    for f in fs:
        total += len(f["strong"])
        for l in f["strong"]:
            if l not in strong:
                strong.append(l)
    show = strong or [l for f in fs for l in f["matched"]]
    # HIGH when the change reverts a guard tied to a published advisory, or removes
    # multiple exact guard lines; a single non-advisory line is MEDIUM.
    has_advisory = any(f.get("ghsa") for f in fs)
    conf = "HIGH" if (has_advisory or total >= 2) else "MEDIUM"
    return {"file": file, "origin": _dec(origin), "refinements": [_dec(f) for f in refinements],
            "removed": show, "confidence": conf, "matches": total}


def scan(diff_text: str) -> dict:
    slice_ = load_slice()
    paths = tracked_paths(slice_)
    findings = detect(build_decisions(slice_), removed_by_file(diff_text, paths))
    if not findings:
        return {"regression": False, "tracked": paths}
    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)
    regs = [_regression_for_file(file, fs) for file, fs in by_file.items()]
    regs.sort(key=lambda r: r["matches"], reverse=True)
    top = regs[0]  # most significant regression -> top-level (back-compat shape)
    return {
        "regression": True, "filename": top["file"], "origin": top["origin"],
        "refinements": top["refinements"], "removed": top["removed"],
        "confidence": top["confidence"], "matches": top["matches"],
        "regressions": regs, "tracked": paths,
    }


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/graph.html", response_class=HTMLResponse)
async def graph():
    p = os.path.join(BASE, "graph.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse(
        "<body style='background:#0a0c10;color:#8b949e;font-family:sans-serif;display:flex;"
        "align-items:center;justify-content:center;height:100vh'><p>No graph yet — click "
        "<b>Rebuild memory</b>.</p></body>"
    )


# ---------------------------------------------------------------- api (pure-python)
@app.get("/api/memory")
async def api_memory():
    slice_ = load_slice()
    paths = tracked_paths(slice_)
    items = []
    for c in sorted(slice_["commits"], key=lambda c: c.get("date") or ""):
        sha = c["short_sha"]
        if sha in LIFECYCLE["forgotten"]:
            continue  # pruned via forget()
        ghsa = []
        for f in c.get("files", []):
            ghsa += GHSA_RE.findall(f.get("patch") or "")
        touches = [p for p in paths if any(match_tracked(f.get("filename"), [p]) for f in c.get("files", []))]
        items.append({
            "sha": sha, "date": (c.get("date") or "")[:10],
            "message": c["message"].splitlines()[0], "ghsa": sorted(set(ghsa)),
            "files": touches,
            "device": "device name" in c["message"].lower(),
            "at_risk": sha in LIFECYCLE["at_risk"],
        })
    advisories = sorted({g for it in items for g in it["ghsa"]})
    return {"repo": slice_["repo"], "subsystem": (paths[0] if paths else ""),
            "tracked": paths, "commits": len(items), "advisories": advisories, "timeline": items}


@app.get("/api/sample/{kind}")
async def api_sample(kind: str):
    fn = "proposed_change.diff" if kind == "bad" else "safe_change.diff"
    p = os.path.join(BASE, "data", fn)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return {"diff": f.read()}
    return {"diff": ""}


@app.post("/api/scan")
async def api_scan(req: ScanReq):
    return scan(req.diff)


@app.post("/api/inspect")
async def api_inspect(req: InspectReq):
    """Content-based inspection for the editor (no diff, no git).

    Given a file's current text, report which past Decisions' guard lines are
    *present* in it and where (so the extension can anchor CodeLens + hover on
    the lines that years of fixes deliberately added). Pure-python, instant.
    """
    slice_ = load_slice()
    paths = tracked_paths(slice_)
    mp = match_tracked(req.path or "", paths)  # which tracked file is this?
    # Collect only real CODE lines present in the file: skip comments and docstring
    # prose so we never flag a doc line as a "guard" (deleting prose reverts nothing).
    present = set()
    in_doc, delim = False, ""
    for raw in (req.content or "").splitlines():
        s = raw.strip()
        if in_doc:
            if delim in s:
                in_doc = False
            continue
        if s.startswith('"""') or s.startswith("'''"):
            d = s[:3]
            if d not in s[3:]:  # docstring not closed on the same line
                in_doc, delim = True, d
            continue
        if is_meaningful(s):
            present.add(s)
    guards = []
    for d in build_decisions(slice_):
        # consider guard lines this decision introduced in the file under inspection;
        # if we couldn't resolve the path, fall back to any tracked file's lines.
        intro = set()
        if mp:
            intro = d.get("introduced_by_file", {}).get(mp, set())
        else:
            for v in d.get("introduced_by_file", {}).values():
                intro |= v
        lines = [l for l in intro if is_strong(l) and l in present]
        if lines:
            guards.append({"sha": d["sha"], "date": d["date"], "message": d["message"],
                           "ghsa": d["ghsa"], "lines": lines})
    guards.sort(key=lambda g: g["date"])
    subsystem = mp or (paths[0] if paths else "")
    return {"subsystem": subsystem, "repo": slice_["repo"], "tracked": paths,
            "is_subsystem": bool(guards) or bool(mp), "guards": guards}


@app.post("/api/check_ref")
async def api_check_ref(req: CheckRefReq):
    slice_ = load_slice()
    repo = slice_["repo"]
    diff = fetch_ref_diff(repo, req.ref)
    if not diff:
        return {"found": False, "repo": repo}
    result = scan(diff)
    result.update({"found": True, "repo": repo, "ref": req.ref, "diff": diff[:8000]})
    return result


# ---------------------------------------------------------------- api (subprocess / graph)
async def _retry(coro_factory):
    """Run an async op; retry once. The first query right after a rebuild re-creates
    Cognee's session tables (and may fail), so a single retry makes it reliable."""
    try:
        return await coro_factory()
    except Exception:
        return await coro_factory()


@app.post("/api/ask")
async def api_ask(req: AskReq):
    """In-process (cognee stays warm => fast). Reads the pre-built graph."""
    async def go():
        res = await search(query_text=req.query, query_type=SearchType.GRAPH_COMPLETION)
        return res[0] if isinstance(res, list) and res else (str(res) if res else "No answer found.")
    try:
        ans = await _retry(go)
    except Exception as e:
        ans = f"(error — try Rebuild memory. {e})"
    return {"answer": ans}


@app.post("/api/explain")
async def api_explain(req: ScanReq):
    slice_ = load_slice()
    paths = tracked_paths(slice_)
    removed_map = removed_by_file(req.diff, paths)
    findings = detect(build_decisions(slice_), removed_map)
    if not findings:
        return {"explanation": None}
    filename = findings[0]["file"]
    removed = removed_map.get(filename, [])
    file_findings = sorted([f for f in findings if f["file"] == filename], key=lambda f: f["date"])
    try:
        text = await _retry(lambda: explain_with_cognee(filename, removed, file_findings[0], file_findings[1:]))
    except Exception as e:
        text = f"(error — try Rebuild memory. {e})"
    return {"explanation": text}


def _kill_db_workers():
    """Kill this server's spawned Cognee DB-worker children so they release the Kuzu
    lock before a subprocess rebuild. Cognee respawns them on the next graph access."""
    me = os.getpid()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={me}\" | ForEach-Object {{ $_.ProcessId }}"],
            capture_output=True, text=True, timeout=15,
        )
        for pid in re.findall(r"\d+", out.stdout or ""):
            if int(pid) != me:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    except Exception:
        pass


async def _rebuild() -> dict:
    """Rebuild in a clean subprocess; release+reset the server's DB state around it so
    fast in-process queries keep working afterward."""
    LIFECYCLE["at_risk"].clear(); LIFECYCLE["forgotten"].clear()  # fresh graph
    try:
        await cognee.disconnect()
    except Exception:
        pass
    await asyncio.to_thread(_kill_db_workers)
    res = await _run_proc("rebuild.py")
    try:
        await cognee.disconnect()  # drop stale cached engines so the next query reconnects fresh
    except Exception:
        pass
    try:
        await search(query_text="warmup", query_type=SearchType.GRAPH_COMPLETION)  # re-create session tables post-rebuild
    except Exception:
        pass
    return res


@app.post("/api/build")
async def api_build():
    return await _rebuild()


@app.post("/api/connect")
async def api_connect(req: ConnectReq):
    """Setup phase: point Lore at a repo + one or more files, ingest their history,
    build the memory."""
    paths = [p.strip() for p in (req.paths or ([req.path] if req.path else [])) if p and p.strip()]
    if not paths:
        return {"ok": False, "error": "Provide at least one file path to track."}
    try:
        slice_ = fetch_multi_slice(req.repo.strip(), paths, (req.keyword or "").strip() or None)
        if not slice_["commits"]:
            return {"ok": False, "error": "No commits found for that repo/path(s). Check owner/repo and the file path(s)."}
        save_slice(slice_)  # becomes the active slice
    except Exception as e:
        return {"ok": False, "error": f"ingest failed: {e}"}
    res = await _rebuild()
    if res.get("ok"):
        res.update({"repo": slice_["repo"], "subsystem": slice_["subsystem_path"],
                    "tracked": slice_.get("tracked_paths", paths), "decisions": len(slice_["commits"])})
    return res


def _decision_node(c, slice_, status):
    code = CodeUnit(path=slice_["subsystem_path"], kind="file", id=CodeUnit.id_for(slice_["subsystem_path"]))
    note = "\n\n[Lore/improve] Confirmed at risk: a proposed change reverts this guard." if status == "at_risk" else ""
    return Decision(
        text=first_line(c["message"]), rationale=c["message"].strip() + note,
        topic="safe_join path-traversal protection", concerns=code,
        decided_on=(c.get("date") or "")[:10], status=status, id=Decision.id_for(c["short_sha"]),
    )


@app.post("/api/confirm")
async def api_confirm(req: ConfirmReq):
    """improve: fold the confirmed regression into memory — upsert the Decision as at_risk."""
    slice_ = load_slice()
    c = next((x for x in slice_["commits"] if x["short_sha"] == req.sha), None)
    if not c:
        return {"ok": False, "error": "unknown decision"}
    try:
        await add_data_points([_decision_node(c, slice_, "at_risk")])
        LIFECYCLE["at_risk"].add(req.sha)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/dismiss")
async def api_dismiss(req: ConfirmReq):
    """feedback: a dismissed flag reverts the Decision back to active."""
    slice_ = load_slice()
    c = next((x for x in slice_["commits"] if x["short_sha"] == req.sha), None)
    if c:
        try:
            await add_data_points([_decision_node(c, slice_, "active")])
        except Exception:
            pass
    LIFECYCLE["at_risk"].discard(req.sha)
    return {"ok": True}


@app.post("/api/forget")
async def api_forget():
    """forget: prune superseded guard decisions from the graph (keep only the newest)."""
    slice_ = load_slice()
    device = [c for c in sorted(slice_["commits"], key=lambda c: c.get("date") or "")
              if "device name" in c["message"].lower()]
    superseded = device[:-1]
    if not superseded:
        return {"ok": True, "forgotten": []}
    ids = [str(Decision.id_for(c["short_sha"])) for c in superseded]
    try:
        eng = await get_graph_engine()
        await eng.delete_nodes(ids)
        LIFECYCLE["forgotten"].update(c["short_sha"] for c in superseded)
        return {"ok": True, "forgotten": [c["short_sha"] for c in superseded]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.on_event("startup")
async def _warmup():
    """Warm the graph in the background so the first real query is fast (~5s, not ~18s cold)."""
    async def go():
        try:
            await search(query_text="warmup", query_type=SearchType.GRAPH_COMPLETION)
        except Exception:
            pass
    asyncio.create_task(go())
