"""Lore — rebuild the graph in a clean process (run as a subprocess by the server).

A fresh process is the reliable way to prune+rebuild the embedded Kuzu graph;
doing it inside the long-lived server trips Kuzu's single-writer file lock.
Prints one `LORE_RESULT {...}` line.
"""
import asyncio
import json
import os

from build_graph import ingest
from cognee import visualize_graph


async def main():
    built = await ingest(prune=True)
    await visualize_graph(os.path.abspath("graph.html"))
    print("LORE_RESULT " + json.dumps({"ok": True, "nodes": len(built["nodes"])}))


if __name__ == "__main__":
    asyncio.run(main())
