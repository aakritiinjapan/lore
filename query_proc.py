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
        from build_graph import load_slice, tracked_paths
        from regress import build_decisions, detect, removed_by_file, explain_with_cognee

        slice_ = load_slice()
        paths = tracked_paths(slice_)
        removed_map = removed_by_file(req["diff"], paths)
        findings = detect(build_decisions(slice_), removed_map)
        if not findings:
            out = {"explanation": None}
        else:
            fn = findings[0]["file"]
            removed = removed_map.get(fn, [])
            by = sorted([f for f in findings if f["file"] == fn], key=lambda f: f["date"])
            out = {"explanation": await explain_with_cognee(fn, removed, by[0], by[1:])}
    else:
        out = {"error": "unknown mode"}

    print("LORE_RESULT " + json.dumps(out))


if __name__ == "__main__":
    asyncio.run(main())
