"""Lore — disk-light ingestion of a scoped slice of any GitHub repo's history.

No clone. Fetches only the commits touching a target file (the "subsystem"),
via the GitHub REST API, caching every response as small JSON under data/cache/.
Also fetches a single PR/commit diff on demand (so the UI can check a change by
number instead of pasting). File contents come from raw.githubusercontent.com.

CLI:  python ingest_github.py              # (re)build the active slice (Werkzeug safe_join)
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CACHE_DIR = os.path.join("data", "cache")
ACTIVE = os.path.join("data", "active_slice.json")  # the currently connected repo's slice

DEFAULT_REPO = "pallets/werkzeug"
DEFAULT_PATH = "src/werkzeug/security.py"
DEFAULT_KEYWORD = r"safe_join|send_from_directory|traversal|GHSA|CVE-|device name|\bCON\b|\bNUL\b"

PR_REF = re.compile(r"#(\d+)")
TOKEN = os.getenv("GITHUB_TOKEN")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs("data", exist_ok=True)


def _get(url, accept="application/vnd.github+json", raw=False):
    is_diff = "diff" in accept
    key = hashlib.sha1((accept + url).encode()).hexdigest()[:16]
    ext = ".diff" if is_diff else (".txt" if raw else ".json")
    path = os.path.join(CACHE_DIR, key + ext)
    if os.path.exists(path):
        data = open(path, encoding="utf-8").read()
        return data if (raw or is_diff) else json.loads(data)
    headers = {"User-Agent": "lore-hackathon", "Accept": accept}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            data = r.read().decode("utf-8")
    except urllib.error.HTTPError:
        return None if (raw or is_diff) else None
    open(path, "w", encoding="utf-8").write(data)
    return data if (raw or is_diff) else json.loads(data)


def fetch_commits_for_path(repo, path, pages=2):
    commits = []
    for page in range(1, pages + 1):
        batch = _get(f"{API}/repos/{repo}/commits?path={path}&per_page=30&page={page}") or []
        commits.extend(batch)
        if len(batch) < 30:
            break
    return commits


def _build_record(repo, c):
    sha = c["sha"]
    detail = _get(f"{API}/repos/{repo}/commits/{sha}") or {}
    cm = detail.get("commit", {}) or {}
    files = []
    for f in detail.get("files", []) or []:
        files.append(
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": (f.get("patch") or "")[:1500],
            }
        )
    msg = cm.get("message", "")
    return {
        "sha": sha,
        "short_sha": sha[:8],
        "date": (cm.get("author") or {}).get("date"),
        "author": (cm.get("author") or {}).get("name"),
        "login": (detail.get("author") or {}).get("login"),
        "message": msg,
        "pr_refs": sorted(set(int(n) for n in PR_REF.findall(msg))),
        "files": files,
    }


def fetch_slice(repo: str, path: str, keyword: str | None = None, limit: int = 8) -> dict:
    """Build a scoped slice: the commits touching `path` (optionally filtered by a
    keyword over commit messages). Works for any public repo."""
    commits = fetch_commits_for_path(repo, path)
    if keyword:
        rx = re.compile(keyword, re.IGNORECASE)
        selected = [c for c in commits if rx.search((c.get("commit") or {}).get("message", ""))]
        if not selected:
            selected = commits  # fall back to recent commits touching the file
    else:
        selected = commits
    selected = selected[:limit]
    return {
        "repo": repo,
        "subsystem_path": path,
        "commits": [_build_record(repo, c) for c in selected],
    }


def save_slice(slice_: dict, path: str = ACTIVE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slice_, f, indent=2)


def fetch_ref_diff(repo: str, ref: str) -> str:
    """Fetch the unified diff for a PR number (e.g. '1234' or '#1234') or a commit SHA."""
    ref = str(ref).strip().lstrip("#")
    if ref.isdigit():
        url = f"{API}/repos/{repo}/pulls/{ref}"
    else:
        url = f"{API}/repos/{repo}/commits/{ref}"
    return _get(url, accept="application/vnd.github.v3.diff") or ""


def main():
    print(f"[ingest] {DEFAULT_REPO} :: {DEFAULT_PATH}")
    slice_ = fetch_slice(DEFAULT_REPO, DEFAULT_PATH, DEFAULT_KEYWORD)
    save_slice(slice_)
    print(f"[saved] {len(slice_['commits'])} commits -> {ACTIVE}")
    for r in sorted(slice_["commits"], key=lambda r: r["date"] or ""):
        print(f"  {(r['date'] or '')[:10]}  {r['short_sha']}  {r['message'].splitlines()[0][:80]}")


if __name__ == "__main__":
    main()
