"""Lore — run a single graph query in a clean subprocess, print the result as JSON.

The server shells out to this for /api/ask and /api/explain so it never holds the
embedded Kuzu lock. Reads stdin JSON {mode, ...}, prints one `LORE_RESULT {...}` line.
"""
import asyncio
import json
import sys


async def main():
    req = json.load(sys.stdin)
    mode = req.get("mode")
    from cognee.low_level import setup

    await setup()

    if mode == "ask":
        from cognee import search, SearchType

        res = await search(query_text=req["query"], query_type=SearchType.GRAPH_COMPLETION)
        ans = res[0] if isinstance(res, list) and res else (str(res) if res else "No answer found.")
        out = {"answer": ans}
    elif mode == "explain":
        from build_graph import load_slice
        from regress import build_decisions, detect, removed_for_subsystem, explain_with_cognee

        slice_ = load_slice()
        fn = slice_["subsystem_path"]
        removed = removed_for_subsystem(req["diff"], fn)
        findings = detect(build_decisions(slice_), removed)
        if not findings:
            out = {"explanation": None}
        else:
            by = sorted(findings, key=lambda f: f["date"])
            out = {"explanation": await explain_with_cognee(fn, removed, by[0], by[1:])}
    else:
        out = {"error": "unknown mode"}

    print("LORE_RESULT " + json.dumps(out))


if __name__ == "__main__":
    asyncio.run(main())
