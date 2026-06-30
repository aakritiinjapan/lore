"""Live end-to-end spike: add_data_points -> search(GRAPH_COMPLETION) -> visualize_graph.
Mini-Lore graph: Issue #800 -> PR #842 (TTL guard) -> Decision, all about search.py.
Run from the lore/ dir so cognee loads lore/.env (single-user mode)."""
import asyncio
import os
from typing import Any, Optional
from pydantic import SkipValidation

import cognee
from cognee.low_level import DataPoint, setup
from cognee.tasks.storage import add_data_points
from cognee import search, SearchType, visualize_graph


class CodeUnit(DataPoint):
    path: str
    kind: str = "file"
    metadata: dict = {"index_fields": ["path"]}


class Issue(DataPoint):
    number: int
    title: str
    body: Optional[str] = None
    metadata: dict = {"index_fields": ["title", "body"]}


class PullRequest(DataPoint):
    number: int
    title: str
    body: Optional[str] = None
    closes: SkipValidation[Any] = None        # -> Issue
    touches: SkipValidation[Any] = None       # -> CodeUnit
    metadata: dict = {"index_fields": ["title", "body"]}


class Decision(DataPoint):
    text: str
    rationale: Optional[str] = None
    concerns: SkipValidation[Any] = None      # -> CodeUnit
    made_in: SkipValidation[Any] = None       # -> PullRequest
    motivated_by: SkipValidation[Any] = None  # -> Issue
    decided_on: str
    metadata: dict = {"index_fields": ["text", "rationale"]}


async def main():
    # 0. initialize cognee (creates relational tables + default user)
    await setup()
    print("[0 setup] ok")

    # 1. clean slate
    try:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        await setup()  # re-create tables after a system prune
        print("[1 prune] ok")
    except Exception as e:
        print("[1 prune] skipped:", repr(e))

    # 2. build a tiny typed graph (the Lore pattern)
    search_py = CodeUnit(path="src/retrieval/search.py")
    issue800 = Issue(
        number=800,
        title="Cache race condition under load",
        body="Concurrent requests corrupt the retrieval cache and return stale results.",
    )
    pr842 = PullRequest(
        number=842,
        title="Add TTL guard to retrieval cache",
        body="Adds a TTL guard around the retrieval cache to fix the race in #800.",
        closes=issue800,
        touches=search_py,
    )
    decision = Decision(
        text="Added a TTL guard to the retrieval cache",
        rationale="Without it, concurrent requests race and corrupt the cache (issue #800); the guard serializes cache writes.",
        concerns=search_py,
        made_in=pr842,
        motivated_by=issue800,
        decided_on="2023-04-01",
    )

    # 3. remember
    await add_data_points([search_py, issue800, pr842, decision])
    print("[2 add_data_points] inserted 4 nodes + edges")

    # 4. recall (the multi-hop "why")
    res = await search(
        query_text="Why does src/retrieval/search.py have a TTL guard? Which PR and issue introduced it?",
        query_type=SearchType.GRAPH_COMPLETION,
    )
    print("\n[3 search GRAPH_COMPLETION] result:\n", res)

    # 5. visualize
    try:
        out_path = os.path.abspath("graph.html")   # visualize_graph needs an absolute path
        await visualize_graph(out_path)
        print("\n[4 visualize_graph] wrote", out_path)
    except Exception as e:
        print("\n[4 visualize_graph] error:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
