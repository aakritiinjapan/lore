"""Lore — build the Cognee graph from the scoped Werkzeug safe_join slice.

Exposes `ingest()` so the regression engine can reuse the exact same graph.
Pipeline: data/werkzeug_safe_join.json -> typed DataPoints + edges ->
add_data_points -> (optional) search(GRAPH_COMPLETION).
"""
import asyncio
import json
import os
from typing import Optional

import cognee
from cognee.low_level import setup
from cognee.tasks.storage import add_data_points
from cognee import search, SearchType, visualize_graph

from models import Author, CodeUnit, Commit, Decision

DATA = os.path.join("data", "active_slice.json")  # the currently connected repo's slice


def load_slice() -> dict:
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)


def first_line(msg: str) -> str:
    return (msg or "").splitlines()[0].strip() if msg else ""


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


def tracked_paths(slice_: dict) -> list:
    """The files Lore watches in this slice. Falls back to the single subsystem
    path for older slices, so existing data keeps working."""
    tp = slice_.get("tracked_paths")
    if tp:
        return list(tp)
    sp = slice_.get("subsystem_path")
    return [sp] if sp else []


def match_tracked(filename: str, paths) -> Optional[str]:
    """Return the tracked path a diff/commit `filename` corresponds to (or None).
    Tolerant of leading a//b/ and repo-relative vs. workspace-relative forms."""
    fn = _norm_path(filename)
    if not fn:
        return None
    for p in paths:
        pp = _norm_path(p)
        if not pp:
            continue
        if fn == pp or fn.endswith("/" + pp) or pp.endswith("/" + fn) or fn.endswith(pp) or pp.endswith(fn):
            return p
    return None


def build_nodes(slice_: dict):
    """Turn the cached commit slice into typed DataPoints with edges.

    Multi-file aware: one CodeUnit per tracked path; each commit/decision links to
    the (primary) tracked file it touches.

    Returns (nodes, code_units_by_path, decisions_in_order).
    """
    paths = tracked_paths(slice_)
    commits_raw = sorted(slice_["commits"], key=lambda c: c.get("date") or "")
    code_units = {p: CodeUnit(path=p, kind="file", id=CodeUnit.id_for(p)) for p in paths}

    authors: dict[str, Author] = {}
    nodes = list(code_units.values())
    decisions_in_order = []

    for c in commits_raw:
        name = c.get("author") or "unknown"
        if name not in authors:
            authors[name] = Author(name=name, github_handle=c.get("login"), id=Author.id_for(name))
            nodes.append(authors[name])

        touched = [p for p in paths if any(match_tracked(f.get("filename"), [p]) for f in c.get("files", []))]
        primary = code_units[touched[0]] if touched else (next(iter(code_units.values()), None))
        topic = (touched[0].rsplit("/", 1)[-1] + " history") if touched else "repo history"

        commit = Commit(
            sha=c["short_sha"],
            message=first_line(c["message"]),
            committed_on=(c.get("date") or "")[:10],
            author=authors[name],
            touches=primary,
            id=Commit.id_for(c["short_sha"]),
        )
        decision = Decision(
            text=first_line(c["message"]),
            rationale=c["message"].strip(),
            topic=topic,
            concerns=primary,
            made_in=commit,
            decided_on=(c.get("date") or "")[:10],
            status="active",
            id=Decision.id_for(c["short_sha"]),
        )
        nodes.extend([commit, decision])
        decisions_in_order.append(decision)

    # Evolution thread: later device-name fixes supersede the earlier ones (kept as a
    # light heuristic; harmless when no such decisions exist).
    device = [d for d in decisions_in_order if "device name" in d.text.lower()]
    for newer, older in zip(device[1:], device):
        newer.supersedes = older
        older.status = "superseded"

    return nodes, code_units, decisions_in_order


async def ingest(prune: bool = True):
    """Build + persist the graph. Returns the built objects."""
    slice_ = load_slice()
    nodes, code_units, decisions = build_nodes(slice_)

    await setup()
    if prune:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        await setup()

    await add_data_points(nodes)
    return {"nodes": nodes, "code_units": code_units,
            "code_unit": next(iter(code_units.values()), None), "decisions": decisions, "slice": slice_}


async def main():
    built = await ingest(prune=True)
    print(f"[remember] add_data_points: {len(built['nodes'])} nodes")

    queries = [
        "Why does safe_join in Werkzeug block Windows special device names, "
        "and how has that protection changed over time?",
        "What is the most recent decision affecting src/werkzeug/security.py and what superseded it?",
    ]
    for q in queries:
        res = await search(query_text=q, query_type=SearchType.GRAPH_COMPLETION)
        text = res[0] if isinstance(res, list) and res else res
        print("\n[recall] Q:", q)
        print("[recall] A:", text)

    try:
        out = os.path.abspath("graph.html")
        await visualize_graph(out)
        print("\n[visualize] wrote", out)
    except Exception as e:
        print("\n[visualize] error:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
