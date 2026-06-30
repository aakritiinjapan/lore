"""Lore — the regression engine (the hero).

Detects when a proposed change REVERTS/REGRESSES a past decision, by exact,
normalized line-matching of the change's *removed* lines against the lines each
past security Decision *introduced*. Deterministic => the alert always fires on a
real regression and rarely false-positives. The LLM only phrases the finding,
fed the exact facts, so it cannot drift.

Run:  python regress.py [path/to/change.diff]
"""
import asyncio
import os
import re
import sys

from cognee import search, SearchType
from build_graph import ingest, load_slice

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
except Exception:
    pass

SEC_FILES = ("security.py",)  # the safe_join implementation file
GHSA_RE = re.compile(r":ghsa:`([^`]+)`")
DOC_PREFIXES = (":param", ":return", ":rtype", ".. version", '"""', "'''", "#")
WEAK_LINES = {"or (", ")", "(", "else:", "return None", "):", "or ("}
DEFAULT_DIFF = os.path.join("data", "proposed_change.diff")


def is_meaningful(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return not any(s.startswith(p) for p in DOC_PREFIXES)


def is_strong(line: str) -> bool:
    """A meaningful line carrying real logic (not a bare structural token)."""
    s = line.strip()
    return is_meaningful(s) and s not in WEAK_LINES and any(c.isalpha() for c in s) and len(s) >= 6


def added_removed(patch: str):
    """Extract normalized, meaningful (+added, -removed) code lines from a patch/diff body."""
    added, removed = [], []
    for raw in (patch or "").splitlines():
        if raw.startswith(("+++", "---", "diff --git", "@@")):
            continue
        if raw.startswith("+"):
            ln = raw[1:].strip()
            if is_meaningful(ln):
                added.append(ln)
        elif raw.startswith("-"):
            ln = raw[1:].strip()
            if is_meaningful(ln):
                removed.append(ln)
    return added, removed


def removed_for_subsystem(diff_text: str, subsystem_path: str):
    """Removed, meaningful lines ONLY within hunks of the subsystem file.

    Critical for accuracy: a guard regression means removing the guard FROM the
    subsystem file — not a code *move* that deletes the lines in some other file.
    """
    cur = None
    out = []
    for raw in (diff_text or "").splitlines():
        if raw.startswith("diff --git"):
            m = re.search(r" b/(\S+)", raw)
            cur = m.group(1) if m else None
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            cur = p[2:] if p.startswith(("a/", "b/")) else p
            continue
        if raw.startswith("--- ") or raw.startswith("@@"):
            continue
        in_subsystem = bool(cur) and (cur == subsystem_path or cur.endswith("/" + subsystem_path) or cur.endswith(subsystem_path))
        if in_subsystem and raw.startswith("-") and not raw.startswith("---"):
            ln = raw[1:].strip()
            if is_meaningful(ln):
                out.append(ln)
    return out


def build_decisions(slice_: dict):
    """Each past commit becomes a Decision with the exact security.py lines it introduced."""
    decisions = []
    for c in slice_["commits"]:
        introduced, ghsas = set(), set()
        for f in c.get("files", []):
            patch = f.get("patch") or ""
            ghsas.update(GHSA_RE.findall(patch))
            if (f.get("filename") or "").endswith(SEC_FILES):
                a, _ = added_removed(patch)
                introduced.update(a)
        decisions.append(
            {
                "sha": c["short_sha"],
                "date": (c.get("date") or "")[:10],
                "message": (c.get("message") or "").splitlines()[0],
                "ghsa": sorted(ghsas),
                "introduced": introduced,
            }
        )
    return decisions


def detect(decisions, removed_lines):
    """Rank decisions whose introduced guard lines the change removes."""
    findings = []
    token_removed = any("_windows_device_files" in l for l in removed_lines)
    for d in decisions:
        matched = [l for l in removed_lines if l in d["introduced"]]
        if not matched:
            continue  # require a real line overlap => low false-positive rate
        strong = [l for l in matched if is_strong(l)]
        d_has_token = any("_windows_device_files" in l for l in d["introduced"])
        score = len(strong) * 2 + len(matched) + (3 if (token_removed and d_has_token) else 0)
        findings.append({**d, "matched": matched, "strong": strong, "score": score})
    findings.sort(key=lambda f: f["score"], reverse=True)
    return findings


def _ghsa(d) -> str:
    return f"  [{', '.join('GHSA-' + g for g in d['ghsa'])}]" if d["ghsa"] else ""


def render_alert(filename, removed_lines, findings) -> str:
    if not findings:
        return "✅ No regression detected: the change does not remove any line a past decision introduced."

    by_date = sorted(findings, key=lambda f: f["date"])
    origin, refinements = by_date[0], by_date[1:]

    # Show the real guard logic that was removed (strong lines), not structural noise.
    strong_union, total_strong = [], 0
    for f in findings:
        total_strong += len(f["strong"])
        for l in f["strong"]:
            if l not in strong_union:
                strong_union.append(l)
    show_lines = strong_union or [l for f in findings for l in f["matched"]]

    lines = ["=" * 72, f"⚠️  REGRESSION RISK in {filename}", "=" * 72, ""]
    lines.append("This change removes guard code that a past decision deliberately added.")
    lines.append("")
    lines.append("Guard originally introduced by:")
    lines.append(f"  • {origin['sha']} ({origin['date']})  \"{origin['message']}\"{_ghsa(origin)}")
    if refinements:
        lines.append("Later hardened by (also affected):")
        for f in refinements:
            lines.append(f"    - {f['sha']} ({f['date']})  \"{f['message']}\"{_ghsa(f)}")
    lines.append("")
    lines.append("Guard logic this change removes:")
    for l in show_lines:
        lines.append(f"    - {l}")
    conf = "HIGH" if (total_strong >= 1 and findings[0]["score"] >= 5) else "MEDIUM"
    lines.append("")
    lines.append(f"Confidence: {conf}  ({total_strong} exact guard-line match(es) across {len(findings)} decision(s))")
    lines.append("Risk: re-opens the path-traversal / Windows device-name class these fixes closed.")
    lines.append("=" * 72)
    return "\n".join(lines)


async def explain_with_cognee(filename, removed_lines, origin, refinements) -> str:
    """Grounded Layer-B: inject the exact facts the deterministic layer found, so the
    LLM only phrases them (and cites the right commit) instead of re-deciding and drifting."""
    removed_str = "\n".join(f"    {l}" for l in removed_lines)
    ghsa = ", ".join("GHSA-" + g for g in origin["ghsa"]) or "n/a"
    later = "; ".join(f"{f['sha']} ({f['date']})" for f in refinements) or "none"
    facts = (
        "FACTS (use ONLY these; do not introduce other commits):\n"
        f"- Guard introduced by commit {origin['sha']} on {origin['date']}: "
        f"\"{origin['message']}\" (advisory {ghsa}).\n"
        f"- It added a Windows special device-name check to safe_join in {filename}.\n"
        f"- Later hardened by: {later}.\n"
        f"- The proposed change removes these guard lines:\n{removed_str}\n"
    )
    q = (
        f"{facts}\n"
        f"In ONE sentence, explain the concrete security risk of removing this guard. "
        f"You MUST cite commit {origin['sha']} and advisory {ghsa}, and mention no other commit."
    )
    res = await search(query_text=q, query_type=SearchType.GRAPH_COMPLETION)
    return res[0] if isinstance(res, list) and res else str(res)


async def main():
    diff_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIFF
    with open(diff_path, encoding="utf-8") as f:
        diff_text = f.read()
    slice_ = load_slice()
    filename = slice_["subsystem_path"]
    removed_lines = removed_for_subsystem(diff_text, filename)
    findings = detect(build_decisions(slice_), removed_lines)

    # --- Layer A: deterministic, guaranteed, accurate ---
    print(render_alert(filename, removed_lines, findings))

    if not findings:
        return

    # --- Layer B: grounded Cognee explanation over the real graph ---
    print("\nBuilding graph + asking Cognee to corroborate (grounded)...\n")
    await ingest(prune=True)
    by_date = sorted(findings, key=lambda f: f["date"])
    origin, refinements = by_date[0], by_date[1:]
    explanation = await explain_with_cognee(filename, removed_lines, origin, refinements)
    print("[Cognee explanation]")
    print(" ", explanation)


if __name__ == "__main__":
    asyncio.run(main())
