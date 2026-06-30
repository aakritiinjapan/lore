"""Lore — build the Cognee graph from the scoped Werkzeug safe_join slice.

Exposes `ingest()` so the regression engine can reuse the exact same graph.
Pipeline: data/werkzeug_safe_join.json -> typed DataPoints + edges ->
add_data_points -> (optional) search(GRAPH_COMPLETION).
"""
import asyncio
import json
import os

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


def build_nodes(slice_: dict):
    """Turn the cached commit slice into typed DataPoints with edges.

    Returns (nodes, code_unit, decisions_in_order).
    """
    commits_raw = sorted(slice_["commits"], key=lambda c: c.get("date") or "")
    code_unit = CodeUnit(path=slice_["subsystem_path"], kind="file", id=CodeUnit.id_for(slice_["subsystem_path"]))

    authors: dict[str, Author] = {}
    nodes = [code_unit]
    decisions_in_order = []

    for c in commits_raw:
        name = c.get("author") or "unknown"
        if name not in authors:
            authors[name] = Author(name=name, github_handle=c.get("login"), id=Author.id_for(name))
            nodes.append(authors[name])

        commit = Commit(
            sha=c["short_sha"],
            message=first_line(c["message"]),
            committed_on=(c.get("date") or "")[:10],
            author=authors[name],
            touches=code_unit,
            id=Commit.id_for(c["short_sha"]),
        )
        decision = Decision(
            text=first_line(c["message"]),
            rationale=c["message"].strip(),
            topic="safe_join path-traversal protection",
            concerns=code_unit,
            made_in=commit,
            decided_on=(c.get("date") or "")[:10],
            status="active",
            id=Decision.id_for(c["short_sha"]),
        )
        nodes.extend([commit, decision])
        decisions_in_order.append(decision)

    # Evolution thread: later device-name fixes supersede the earlier ones.
    device = [d for d in decisions_in_order if "device name" in d.text.lower()]
    for newer, older in zip(device[1:], device):
        newer.supersedes = older
        older.status = "superseded"

    return nodes, code_unit, decisions_in_order


async def ingest(prune: bool = True):
    """Build + persist the graph. Returns the built objects."""
    slice_ = load_slice()
    nodes, code_unit, decisions = build_nodes(slice_)

    await setup()
    if prune:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        await setup()

    await add_data_points(nodes)
    return {"nodes": nodes, "code_unit": code_unit, "decisions": decisions, "slice": slice_}


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
