// Lore — VS Code extension.
//
// Lore is codebase memory. This extension surfaces that memory natively inside the
// editor, in five places — each optional, each in its own spot:
//   1. Inline regression squiggle  — delete a guard a past decision added, get flagged.
//   2. Hover "why is this here?"    — hover a guard line to see the decision behind it.
//   3. CodeLens                     — "Protected by N Lore decisions" above the function.
//   4. Lore sidebar                 — current-file decisions + an optional Ask chat.
//   5. Knowledge Graph panel        — an optional, on-demand decision graph.
//
// Architecture: the webviews NEVER touch the network. The extension host (Node) is the
// only HTTP client; it proxies every request to the local Lore service. Detection reuses
// the exact same /api/scan engine the web app uses, so a squiggle and a CI alert agree.

const vscode = require("vscode");
const http = require("http");
const https = require("https");
const { URL } = require("url");

// ----------------------------------------------------------------------------- config
function serviceBase() {
  return vscode.workspace.getConfiguration("lore").get("serviceUrl") || "http://127.0.0.1:8765";
}
function regressionSeverity() {
  const s = (vscode.workspace.getConfiguration("lore").get("regressionSeverity") || "Error").toLowerCase();
  return s === "warning" ? vscode.DiagnosticSeverity.Warning
       : s === "information" ? vscode.DiagnosticSeverity.Information
       : vscode.DiagnosticSeverity.Error;
}

// ------------------------------------------------------------------- host HTTP client
// The single network chokepoint. Webviews post a message; we make the call here.
function apiRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, serviceBase());
    const lib = u.protocol === "https:" ? https : http;
    const data = body != null ? JSON.stringify(body) : null;
    const req = lib.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method: method || "GET",
        headers: Object.assign(
          { "Content-Type": "application/json" },
          data ? { "Content-Length": Buffer.byteLength(data) } : {}
        ),
        timeout: 120000,
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          try { resolve(JSON.parse(chunks)); }
          catch (e) { resolve({ raw: chunks }); }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("Lore service request timed out")));
    if (data) req.write(data);
    req.end();
  });
}

// --------------------------------------------------------------------- per-doc memory
// For each open document we remember the guard lines that were present when we first
// saw it (the baseline). If one of those strong guard lines later disappears, that's a
// candidate regression we confirm with the server.
const STATE = new Map(); // uri.toString() -> { baseText, guards:[{text,sha,date,ghsa,message}], subsystem, repo, hasGuards, scan }
const DEBOUNCE = new Map();

const norm = (s) => (s || "").trim();
const docRelPath = (doc) => {
  const rel = vscode.workspace.asRelativePath(doc.uri, false);
  return (rel || doc.fileName || "").replace(/\\/g, "/");
};

async function ensureInspected(doc) {
  const key = doc.uri.toString();
  if (STATE.has(key)) return STATE.get(key);
  if (doc.lineCount > 6000 || doc.getText().length > 500000) { // skip very large files
    const st = { baseText: "", guards: [], subsystem: "", repo: "", hasGuards: false, scan: null };
    STATE.set(key, st); return st;
  }
  let res;
  try {
    res = await apiRequest("POST", "/api/inspect", { path: docRelPath(doc), content: doc.getText() });
  } catch (e) {
    return null; // service down — stay silent, don't nag
  }
  const guards = [];
  for (const g of (res && res.guards) || []) {
    for (const text of g.lines || []) {
      guards.push({ text: norm(text), sha: g.sha, date: g.date, ghsa: g.ghsa || [], message: g.message });
    }
  }
  const st = {
    baseText: doc.getText(),
    guards,
    subsystem: res ? res.subsystem : "",
    repo: res ? res.repo : "",
    hasGuards: !!(res && res.is_subsystem && guards.length),
    scan: null,
  };
  STATE.set(key, st);
  return st;
}

// Decisions present in the *current* text, deduped + ordered by date. Used by sidebar.
function decisionsFor(st) {
  const by = new Map();
  for (const g of st.guards) {
    if (!by.has(g.sha)) by.set(g.sha, { sha: g.sha, date: g.date, ghsa: g.ghsa, message: g.message });
  }
  return [...by.values()].sort((a, b) => (a.date || "").localeCompare(b.date || ""));
}

// Find the current line number (0-based) of a guard whose stripped text matches. Guards
// move as the file is edited, so we locate by text, not a frozen line number.
function lineOf(doc, text) {
  for (let i = 0; i < doc.lineCount; i++) {
    if (norm(doc.lineAt(i).text) === text) return i;
  }
  return -1;
}

// Where to place a regression squiggle for lines that are now *gone*: anchor on the
// nearest still-present baseline line, so the warning sits right where the guard was.
function removalAnchor(doc, st, removedSet) {
  const baseLines = st.baseText.split(/\r?\n/);
  let first = -1;
  for (let i = 0; i < baseLines.length; i++) {
    if (removedSet.has(norm(baseLines[i]))) { first = i; break; }
  }
  if (first === -1) return 0;
  for (let i = first - 1; i >= 0; i--) {           // search upward for a stable anchor
    const ln = lineOf(doc, norm(baseLines[i]));
    if (ln !== -1) return ln;
  }
  for (let i = first + 1; i < baseLines.length; i++) { // then downward
    const ln = lineOf(doc, norm(baseLines[i]));
    if (ln !== -1) return Math.max(0, ln - 1);
  }
  return 0;
}

// ------------------------------------------------------------- surface 1: diagnostics
let diagnostics;

async function refreshDiagnostics(doc) {
  if (!doc || doc.uri.scheme !== "file") return;
  const st = await ensureInspected(doc);
  if (!st || !st.hasGuards) { diagnostics.delete(doc.uri); return; }

  const present = new Set();
  for (let i = 0; i < doc.lineCount; i++) present.add(norm(doc.lineAt(i).text));
  const removed = [...new Set(st.guards.map((g) => g.text))].filter((t) => !present.has(t));

  if (!removed.length) { st.scan = null; diagnostics.delete(doc.uri); refreshUi(); return; }

  // Build the exact diff the web app / CI would scan, then ask the authoritative engine.
  const sub = st.subsystem || docRelPath(doc);
  const diff =
    `diff --git a/${sub} b/${sub}\n--- a/${sub}\n+++ b/${sub}\n@@ -1 +1 @@\n` +
    removed.map((l) => "-" + l + "\n").join("");
  let scan;
  try { scan = await apiRequest("POST", "/api/scan", { diff }); }
  catch (e) { return; }
  st.scan = scan;

  if (!scan || !scan.regression) { diagnostics.delete(doc.uri); refreshUi(); return; }

  const anchor = removalAnchor(doc, st, new Set(removed));
  const range = doc.lineAt(Math.min(anchor, doc.lineCount - 1)).range;
  const o = scan.origin || {};
  const ghsa = (o.ghsa || []).map((g) => "GHSA-" + g).join(", ") || "n/a";
  const ref = (scan.refinements || []).map((f) => `${f.sha} (${f.date})`).join("; ");
  const msg =
    `Regression risk — this removes a guard a past decision deliberately added.\n\n` +
    `Introduced by ${o.sha} (${o.date}): "${o.message}"  [${ghsa}]\n` +
    (ref ? `Later hardened by: ${ref}\n` : "") +
    `Removed guard logic:\n` + removed.map((l) => "    - " + l).join("\n") + `\n\n` +
    `Risk: re-opens the path-traversal / Windows device-name class these fixes closed.\n` +
    `Confidence: ${scan.confidence}.`;

  const d = new vscode.Diagnostic(range, msg, regressionSeverity());
  d.source = "Lore";
  d.code = o.sha ? { value: o.sha, target: vscode.Uri.parse(`${serviceBase()}/`) } : undefined;
  diagnostics.set(doc.uri, [d]);
  refreshUi();
}

// ------------------------------------------------------ surface 1b: quick-fix (restore)
// Lore doesn't *guess* a regression fix — it knows it. The removed guard is the fix.
// Reconstruct the exact deleted block (original indentation) from the baseline via a
// prefix/suffix diff, so "Restore" puts back precisely what was there.
function removedBlock(doc, st) {
  const base = st.baseText.split(/\r?\n/);
  const cur = [];
  for (let i = 0; i < doc.lineCount; i++) cur.push(doc.lineAt(i).text);
  const sb = base.map(norm), sc = cur.map(norm);
  let p = 0;
  while (p < sb.length && p < sc.length && sb[p] === sc[p]) p++;
  let s = 0;
  while (s < sb.length - p && s < sc.length - p && sb[sb.length - 1 - s] === sc[sc.length - 1 - s]) s++;
  const removed = base.slice(p, base.length - s); // lines present in baseline, gone now
  if (!removed.length) return null;
  const removedStripped = new Set(removed.map(norm));
  if (!st.guards.some((g) => removedStripped.has(g.text))) return null; // must include a tracked guard
  return { text: removed.join("\n") + "\n", insertLine: Math.min(p, doc.lineCount) };
}

// All deleted line-runs between baseline and current (via LCS), each with where to
// re-insert it. Powers "Restore all guards" when guards were removed in several places.
function diffDeletions(base, cur) {
  const n = base.length, m = cur.length;
  const a = base.map(norm), b = cur.map(norm);
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const dels = [];
  let i = 0, j = 0, run = null;
  while (i < n && j < m) {
    if (a[i] === b[j]) { if (run) { dels.push(run); run = null; } i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { if (!run) run = { at: j, lines: [] }; run.lines.push(base[i]); i++; }
    else { if (run) { dels.push(run); run = null; } j++; }
  }
  while (i < n) { if (!run) run = { at: j, lines: [] }; run.lines.push(base[i]); i++; }
  if (run) dels.push(run);
  return dels;
}

const fixProvider = {
  provideCodeActions(doc, range, context) {
    const st = STATE.get(doc.uri.toString());
    if (!st || !st.scan || !st.scan.regression) return [];
    const ours = (context.diagnostics || []).filter((d) => d.source === "Lore");
    if (!ours.length) return [];
    const block = removedBlock(doc, st);
    if (!block) return [];
    const o = st.scan.origin || {};
    const adv = (o.ghsa || []).map((g) => "GHSA-" + g).join(", ");
    const action = new vscode.CodeAction(
      `Lore: Restore the guard removed from ${o.sha}${adv ? " (" + adv + ")" : ""}`,
      vscode.CodeActionKind.QuickFix
    );
    action.diagnostics = ours;
    action.isPreferred = true;
    const edit = new vscode.WorkspaceEdit();
    const at = block.insertLine >= doc.lineCount
      ? new vscode.Position(doc.lineCount, 0)
      : new vscode.Position(block.insertLine, 0);
    edit.insert(doc.uri, at, block.text);
    action.edit = edit;
    return [action];
  },
};

// ------------------------------------------------------------------ surface 2: hover
const hoverProvider = {
  async provideHover(doc, position) {
    const st = await ensureInspected(doc);
    if (!st || !st.hasGuards) return null;
    const text = norm(doc.lineAt(position.line).text);
    const g = st.guards.find((x) => x.text === text);
    if (!g) return null;
    const ghsa = (g.ghsa || []).map((x) => "GHSA-" + x).join(", ");
    const md = new vscode.MarkdownString(undefined, true);
    md.isTrusted = true;
    md.appendMarkdown(`$(shield) **Lore — why this exists**\n\n`);
    md.appendMarkdown(`Introduced by decision \`${g.sha}\` on **${g.date}**.\n\n`);
    md.appendMarkdown(`> ${g.message}\n\n`);
    if (ghsa) {
      md.appendMarkdown(`$(warning) Security fix — advisory **${ghsa}**.\n\n`);
      md.appendMarkdown(`Removing this line risks re-opening the vulnerability it closed.\n\n`);
    } else {
      md.appendMarkdown(`Removing this line would revert part of that change.\n\n`);
    }
    const q = encodeURIComponent(JSON.stringify([{ sha: g.sha, message: g.message, ghsa: g.ghsa }]));
    md.appendMarkdown(`[$(comment-discussion) Explain in depth](command:lore.why?${q}) · `);
    md.appendMarkdown(`[$(graph) Knowledge graph](command:lore.graph)`);
    return new vscode.Hover(md, doc.lineAt(position.line).range);
  },
};

// --------------------------------------------------------------- surface 3: CodeLens
const _codeLensEmitter = new vscode.EventEmitter();
const codeLensProvider = {
  onDidChangeCodeLenses: _codeLensEmitter.event,
  async provideCodeLenses(doc) {
    const st = await ensureInspected(doc);
    if (!st || !st.hasGuards) return [];
    // Map each present guard line to its current line number, then group by the
    // enclosing `def`/function so the lens sits above the function it protects.
    const defLines = [];
    for (let i = 0; i < doc.lineCount; i++) {
      if (/^\s*(def|class|function|func|fn)\s/.test(doc.lineAt(i).text)) defLines.push(i);
    }
    const groups = new Map(); // defLine -> Set(sha)
    const detail = new Map(); // sha -> {date, ghsa, message}
    for (const g of st.guards) {
      const ln = lineOf(doc, g.text);
      if (ln === -1) continue;
      let owner = 0;
      for (const d of defLines) { if (d <= ln) owner = d; else break; }
      if (!groups.has(owner)) groups.set(owner, new Set());
      groups.get(owner).add(g.sha);
      detail.set(g.sha, { date: g.date, ghsa: g.ghsa, message: g.message });
    }
    const lenses = [];
    for (const [defLine, shas] of groups) {
      const decisions = [...shas].map((s) => Object.assign({ sha: s }, detail.get(s)))
        .sort((a, b) => (a.date || "").localeCompare(b.date || ""));
      const advisories = new Set();
      decisions.forEach((d) => (d.ghsa || []).forEach((g) => advisories.add(g)));
      const n = decisions.length;
      const a = advisories.size;
      const title = `$(shield) Protected by ${n} Lore decision${n > 1 ? "s" : ""}` +
        (a ? ` · ${a} security advisor${a > 1 ? "ies" : "y"}` : "");
      lenses.push(new vscode.CodeLens(doc.lineAt(defLine).range, {
        title,
        command: "lore.showDecisions",
        arguments: [decisions],
      }));
    }
    return lenses;
  },
};

// --------------------------------------------------------------- surface 4: sidebar
let sidebarView = null;

// Short rolling conversation memory so follow-up questions keep their thread. The
// /api/ask endpoint is stateless, so we prepend the recent exchange to each query.
const CONVO = [];
function pushConvo(role, text) {
  CONVO.push({ role, text });
  if (CONVO.length > 8) CONVO.splice(0, CONVO.length - 8);
}

// Build the query sent to Lore: recent conversation + (if the active file has a
// regression) the exact removed guard, so answers stay specific, not generic.
function buildAskQuery(query) {
  let pre = "";
  const hist = CONVO.slice(0, -1).slice(-4); // last ~2 exchanges, excluding the current msg
  if (hist.length) {
    pre += "Conversation so far (for context on follow-up questions):\n" +
      hist.map((m) => (m.role === "user" ? "User: " : "Lore: ") + m.text).join("\n") + "\n\n";
  }
  const ed = vscode.window.activeTextEditor;
  const st = ed && STATE.get(ed.document.uri.toString());
  if (st && st.scan && st.scan.regression) {
    const o = st.scan.origin || {};
    const ghsa = (o.ghsa || []).map((g) => "GHSA-" + g).join(", ") || "n/a";
    const removed = (st.scan.removed || []).map((l) => "    " + l).join("\n");
    pre += `CONTEXT: ${st.subsystem} currently has a regression — a change removed these guard lines ` +
      `that decision ${o.sha} (${o.date}, advisory ${ghsa}) introduced:\n${removed}\n\n`;
  }
  return pre + `Answer this question (be specific to the context above when relevant; avoid generic advice):\n${query}`;
}

// The exact, deterministic fix for a regression: re-add the precise lines that were
// removed. No LLM guess — Lore remembers the guard, so it shows it verbatim.
function buildFixAnswer() {
  const ed = vscode.window.activeTextEditor;
  const st = ed && STATE.get(ed.document.uri.toString());
  if (!st || !st.scan || !st.scan.regression) return "No active regression in the current file.";
  const o = st.scan.origin || {};
  const ghsa = (o.ghsa || []).map((g) => "GHSA-" + g).join(", ") || "no advisory on record";
  const block = ed ? removedBlock(ed.document, st) : null;
  const code = block ? block.text.replace(/\s+$/, "") : (st.scan.removed || []).join("\n");
  const ref = (st.scan.refinements || []).map((f) => `${f.sha} (${f.date})`).join("; ");
  return (
    `The fix is to restore the guard you removed. Re-add these exact lines to safe_join in ${st.subsystem}:\n\n` +
    code + `\n\n` +
    `Why: this guard was introduced by ${o.sha} (${o.date}) for advisory ${ghsa}` +
    (ref ? `, and later hardened by ${ref}` : "") + `. ` +
    `Leaving it out re-opens the path-traversal / Windows device-name vulnerability it closed.\n\n` +
    `Tip: click the 💡 lightbulb on the squiggle and choose “Restore the guard …” to re-insert it automatically.`
  );
}

// Fix-intent: questions where the user wants the regression undone (vs. open-ended Q&A).
const FIX_INTENT = /\b(fix|revert|restore|undo|repair|put\s?back|re-?add|how.*(solve|resolve))\b/i;

const sidebarProvider = {
  resolveWebviewView(view) {
    sidebarView = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = sidebarHtml(view.webview);
    view.webview.onDidReceiveMessage(async (m) => {
      if (m.type === "ask") {
        const query = (m.query || "").trim();
        if (!query) return;
        pushConvo("user", query);
        // If the current file has a regression and the user is asking how to fix it,
        // answer deterministically with the exact removed lines (no LLM round-trip).
        const ed = vscode.window.activeTextEditor;
        const st = ed && STATE.get(ed.document.uri.toString());
        if (st && st.scan && st.scan.regression && FIX_INTENT.test(query)) {
          const a = buildFixAnswer();
          pushConvo("assistant", a);
          view.webview.postMessage({ type: "answer", turn: m.turn, answer: a });
          return;
        }
        try {
          const r = await apiRequest("POST", "/api/ask", { query: buildAskQuery(query) });
          const a = (r && r.answer) || "No answer found.";
          pushConvo("assistant", a);
          view.webview.postMessage({ type: "answer", turn: m.turn, answer: a });
        } catch (e) {
          view.webview.postMessage({ type: "answer", turn: m.turn, answer: "⚠️ Could not reach the Lore service. Is it running on " + serviceBase() + "?" });
        }
      } else if (m.type === "clear") {
        CONVO.length = 0;
      } else if (m.type === "graph") {
        vscode.commands.executeCommand("lore.graph");
      } else if (m.type === "ready") {
        updateSidebarForActive();
      }
    });
    updateSidebarForActive();
  },
};

function updateSidebarForActive() {
  if (!sidebarView) return;
  const ed = vscode.window.activeTextEditor;
  if (!ed) { sidebarView.webview.postMessage({ type: "context", data: null }); return; }
  const st = STATE.get(ed.document.uri.toString());
  if (!st || !st.hasGuards) { sidebarView.webview.postMessage({ type: "context", data: null }); return; }
  const data = {
    file: docRelPath(ed.document),
    subsystem: st.subsystem,
    repo: st.repo,
    decisions: decisionsFor(st),
    regression: st.scan && st.scan.regression ? st.scan : null,
  };
  sidebarView.webview.postMessage({ type: "context", data });
}

// Status-bar signal for the active file: 🛡 N decisions, and ⚠ if a guard was removed.
let statusBar;
function updateStatusBar() {
  if (!statusBar) return;
  const ed = vscode.window.activeTextEditor;
  const st = ed && STATE.get(ed.document.uri.toString());
  if (!st || !st.hasGuards) { statusBar.hide(); return; }
  const n = decisionsFor(st).length;
  const reg = st.scan && st.scan.regression;
  statusBar.text = `$(shield) Lore: ${n} decision${n > 1 ? "s" : ""}` + (reg ? "  $(warning) regression" : "");
  statusBar.tooltip = reg
    ? "Lore: a guard a past decision added was removed — click to view"
    : `Lore: ${n} past decision${n > 1 ? "s" : ""} protect this file — click to view`;
  statusBar.backgroundColor = reg ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  statusBar.command = "lore.showDecisions";
  statusBar.show();
}

function refreshUi() { updateSidebarForActive(); updateStatusBar(); }

// --------------------------------------------------------- surface 5: graph panel
let graphPanel = null;

async function openGraph() {
  let mem;
  try { mem = await apiRequest("GET", "/api/memory"); }
  catch (e) { vscode.window.showErrorMessage("Lore: could not reach the service at " + serviceBase()); return; }

  if (graphPanel) { graphPanel.reveal(vscode.ViewColumn.Beside); }
  else {
    graphPanel = vscode.window.createWebviewPanel(
      "loreGraph", "Lore — Knowledge Graph", vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true }
    );
    graphPanel.onDidDispose(() => (graphPanel = null));
  }
  graphPanel.webview.html = graphHtml(graphPanel.webview, mem);
}

// ----------------------------------------------------------------------- activation
function activate(context) {
  diagnostics = vscode.languages.createDiagnosticCollection("lore");
  context.subscriptions.push(diagnostics);

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  context.subscriptions.push(statusBar);

  const selector = [{ scheme: "file" }];
  context.subscriptions.push(
    vscode.languages.registerHoverProvider(selector, hoverProvider),
    vscode.languages.registerCodeLensProvider(selector, codeLensProvider),
    vscode.languages.registerCodeActionsProvider(selector, fixProvider, {
      providedCodeActionKinds: [vscode.CodeActionKind.QuickFix],
    }),
    vscode.window.registerWebviewViewProvider("lore.sidebar", sidebarProvider, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  // commands
  context.subscriptions.push(
    vscode.commands.registerCommand("lore.graph", openGraph),
    vscode.commands.registerCommand("lore.scanFile", async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed) return;
      await refreshDiagnostics(ed.document); // compare current buffer against the baseline
      _codeLensEmitter.fire();
      const st = STATE.get(ed.document.uri.toString());
      if (!st || !st.hasGuards) vscode.window.showInformationMessage("Lore: no tracked decisions in this file.");
      else if (st.scan && st.scan.regression) vscode.window.showWarningMessage("Lore: regression risk detected — see the squiggle and the Lore panel.");
      else vscode.window.showInformationMessage(`Lore: ${decisionsFor(st).length} decision(s) intact in this file.`);
    }),
    vscode.commands.registerCommand("lore.showDecisions", async () => {
      await vscode.commands.executeCommand("lore.sidebar.focus");
    }),
    vscode.commands.registerCommand("lore.restoreAll", async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed) return;
      const doc = ed.document;
      const st = STATE.get(doc.uri.toString());
      if (!st || !st.hasGuards) { vscode.window.showInformationMessage("Lore: no tracked decisions in this file."); return; }
      const base = st.baseText.split(/\r?\n/);
      const cur = [];
      for (let i = 0; i < doc.lineCount; i++) cur.push(doc.lineAt(i).text);
      const guardSet = new Set(st.guards.map((g) => g.text));
      let dels;
      if (base.length < 2000 && cur.length < 2000) dels = diffDeletions(base, cur);
      else { const b = removedBlock(doc, st); dels = b ? [{ at: b.insertLine, lines: b.text.replace(/\n$/, "").split("\n") }] : []; }
      const restore = dels.filter((d) => d.lines.some((l) => guardSet.has(norm(l))));
      if (!restore.length) { vscode.window.showInformationMessage("Lore: all guards are present in this file."); return; }
      const edit = new vscode.WorkspaceEdit();
      for (const d of restore) {
        const at = d.at >= cur.length ? new vscode.Position(doc.lineCount, 0) : new vscode.Position(d.at, 0);
        edit.insert(doc.uri, at, d.lines.join("\n") + "\n");
      }
      await vscode.workspace.applyEdit(edit);
      await refreshDiagnostics(doc);
      _codeLensEmitter.fire();
      vscode.window.showInformationMessage(`Lore: restored ${restore.length} guard region${restore.length > 1 ? "s" : ""}.`);
    }),
    vscode.commands.registerCommand("lore.ask", async (preset) => {
      const query = typeof preset === "string" ? preset : await vscode.window.showInputBox({
        prompt: "Ask Lore about this codebase", placeHolder: "Why does safe_join reject Windows device names?",
      });
      if (!query) return;
      await vscode.commands.executeCommand("lore.sidebar.focus");
      if (sidebarView) sidebarView.webview.postMessage({ type: "run", query });
    }),
    vscode.commands.registerCommand("lore.why", async (arg) => {
      const d = Array.isArray(arg) ? arg[0] : arg;
      const ghsa = d && d.ghsa && d.ghsa.length ? ` (advisory GHSA-${d.ghsa[0]})` : "";
      const q = `Why does this guard exist? Explain the security risk of removing decision ${d ? d.sha : ""}${ghsa}. Cite the commit and advisory.`;
      await vscode.commands.executeCommand("lore.ask", q);
    })
  );

  // live triggers
  const onChange = (doc) => {
    if (!doc || doc.uri.scheme !== "file") return;
    const key = doc.uri.toString();
    clearTimeout(DEBOUNCE.get(key));
    DEBOUNCE.set(key, setTimeout(() => { refreshDiagnostics(doc); _codeLensEmitter.fire(); }, 400));
  };
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((e) => onChange(e.document)),
    vscode.workspace.onDidOpenTextDocument((doc) => refreshDiagnostics(doc)),
    vscode.workspace.onDidSaveTextDocument((doc) => refreshDiagnostics(doc)),
    vscode.workspace.onDidCloseTextDocument((doc) => { STATE.delete(doc.uri.toString()); diagnostics.delete(doc.uri); }),
    vscode.window.onDidChangeActiveTextEditor((ed) => { if (ed) refreshDiagnostics(ed.document); refreshUi(); })
  );

  if (vscode.window.activeTextEditor) refreshDiagnostics(vscode.window.activeTextEditor.document);
}

function deactivate() {}

// --------------------------------------------------------------------- webview HTML
function nonce() {
  let s = "";
  const c = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 24; i++) s += c.charAt(Math.floor(Math.random() * c.length));
  return s;
}

function sidebarHtml(webview) {
  const n = nonce();
  const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${n}';`;
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  :root { color-scheme: dark light; }
  html,body { height:100%; }
  body { margin:0; display:flex; flex-direction:column; font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); }
  .muted { opacity:.6; font-size:12px; line-height:1.5; }
  /* collapsible context */
  #top { flex:0 0 auto; padding:8px 12px 0; }
  #ctxHead { display:flex; align-items:center; gap:6px; cursor:pointer; font-size:11px; text-transform:uppercase; letter-spacing:.06em; opacity:.8; user-select:none; padding:3px 0; }
  #ctxHead .caret { transition:transform .15s; font-size:10px; opacity:.7; }
  #ctxHead.collapsed .caret { transform:rotate(-90deg); }
  #ctx { margin-top:6px; }
  #ctx.hidden { display:none; }
  .card { border:1px solid var(--vscode-panel-border); border-radius:6px; padding:9px 10px; margin-bottom:8px; background: var(--vscode-editorWidget-background); }
  .dec { display:flex; gap:8px; align-items:flex-start; padding:6px 0; border-top:1px solid var(--vscode-panel-border); }
  .dec:first-child{ border-top:none; }
  .sha { font-family: var(--vscode-editor-font-family); color: var(--vscode-textLink-foreground); font-size:12px; }
  .date { opacity:.6; font-size:11px; }
  .msg { font-size:12.5px; line-height:1.45; margin-top:2px; }
  .chip { display:inline-block; font-size:10px; padding:1px 6px; border-radius:10px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); margin-top:4px; }
  .danger { border-color:#e5534b; background: rgba(229,83,75,.10); }
  .danger .t { color:#ff8a80; font-weight:600; font-size:12.5px; }
  /* chat transcript */
  #chat { flex:1 1 auto; overflow-y:auto; padding:6px 12px; }
  #empty { opacity:.55; font-size:12px; line-height:1.6; margin-top:8px; }
  .turn { display:flex; margin:7px 0; }
  .turn.user { justify-content:flex-end; }
  .bubble { max-width:88%; padding:8px 10px; border-radius:11px; font-size:12.5px; line-height:1.5; white-space:pre-wrap; word-break:break-word; }
  .user .bubble { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border-bottom-right-radius:3px; }
  .bot .bubble { background: var(--vscode-editorWidget-background); border:1px solid var(--vscode-panel-border); border-bottom-left-radius:3px; }
  .bot.pending .bubble { opacity:.65; }
  .dots::after { content:'…'; animation: blink 1.2s steps(4) infinite; }
  @keyframes blink { 0%{opacity:.3} 50%{opacity:1} 100%{opacity:.3} }
  /* input bar */
  #bar { flex:0 0 auto; padding:8px 12px 10px; border-top:1px solid var(--vscode-panel-border); }
  #ask { width:100%; box-sizing:border-box; padding:7px; border-radius:5px; border:1px solid var(--vscode-input-border,transparent); background: var(--vscode-input-background); color: var(--vscode-input-foreground); font-size:12.5px; resize:none; max-height:120px; }
  .row { display:flex; gap:6px; margin-top:6px; align-items:center; }
  .btn { cursor:pointer; border:1px solid var(--vscode-button-border,transparent); background: var(--vscode-button-secondaryBackground, #2a2d2e); color: var(--vscode-button-secondaryForeground,#fff); border-radius:5px; padding:6px 10px; font-size:12px; }
  .btn:hover{ background: var(--vscode-button-secondaryHoverBackground,#3a3d3e); }
  .btn.primary { flex:1; background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  .btn.primary:hover{ background: var(--vscode-button-hoverBackground); }
  .btn.icon { padding:6px 8px; }
  .fixbtn { width:100%; margin-top:8px; }
</style></head><body>
  <div id="top">
    <div id="ctxHead"><span class="caret">▾</span> <span id="ctxTitle">This file</span></div>
    <div id="ctx"><p class="muted">Open a file Lore has memory of (e.g. the demo <code>security.py</code>) to see the decisions protecting it.</p></div>
  </div>
  <div id="chat"><div id="empty">Ask Lore anything about this codebase — why a guard exists, what a decision protects, or how to fix a regression. Follow-up questions keep their thread.</div></div>
  <div id="bar">
    <textarea id="ask" rows="1" placeholder="Ask Lore…  (Enter to send · Shift+Enter for newline)"></textarea>
    <div class="row">
      <button id="send" class="btn primary">Ask</button>
      <button id="graphBtn" class="btn icon" title="Open Knowledge Graph">🗺</button>
      <button id="clear" class="btn icon" title="Clear conversation">🗑</button>
    </div>
  </div>
<script nonce="${n}">
  const vscode = acquireVsCodeApi();
  const ctx = document.getElementById('ctx');
  const ctxHead = document.getElementById('ctxHead');
  const ctxTitle = document.getElementById('ctxTitle');
  const chat = document.getElementById('chat');
  const ask = document.getElementById('ask');
  let turnId = 0;
  let started = false;
  const state = vscode.getState() || { turns: [], collapsed:false };

  function esc(s){ return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function save(){ vscode.setState(state); }

  function setCollapsed(c){ ctxHead.classList.toggle('collapsed', c); ctx.classList.toggle('hidden', c); state.collapsed=c; save(); }
  ctxHead.onclick = ()=> setCollapsed(!ctx.classList.contains('hidden'));

  function renderCtx(d){
    if(!d){ ctx.innerHTML = '<p class="muted">Open a file Lore has memory of (e.g. the demo <code>security.py</code>) to see the decisions protecting it.</p>'; ctxTitle.textContent='This file'; return; }
    ctxTitle.textContent = (d.subsystem||d.file||'This file').split('/').pop() + ' · ' + d.decisions.length + ' decisions';
    let h = '';
    if(d.regression){
      const o=d.regression.origin||{}; const g=(o.ghsa||[]).map(x=>'GHSA-'+x).join(', ');
      h += '<div class="card danger"><div class="t">⚠ Regression risk in this file</div>'
        + '<div class="msg">Removes a guard from <b>'+esc(o.sha)+'</b> ('+esc(o.date)+')'+(g?' · '+esc(g):'')+'.</div>'
        + '<button class="btn fixbtn">How do I fix this?</button></div>';
    }
    h += '<div class="card"><div class="msg"><b>'+esc(d.subsystem||d.file)+'</b><div class="date">'+esc(d.repo||'')+'</div></div></div>';
    for(const x of d.decisions){
      const g=(x.ghsa||[]).map(y=>'GHSA-'+y).join(', ');
      h += '<div class="card"><div class="dec"><div class="sha">'+esc(x.sha)+'</div><div><div class="msg">'+esc(x.message)+'</div>'
        + '<span class="date">'+esc(x.date)+'</span>'+(g?'<br><span class="chip">'+esc(g)+'</span>':'')+'</div></div></div>';
    }
    ctx.innerHTML = h;
    const fb = ctx.querySelector('.fixbtn');
    if(fb) fb.onclick = ()=> run('How do I fix this regression? Show the exact lines I should restore.');
  }

  function addBubble(role, text, turn){
    const t = document.createElement('div');
    t.className = 'turn ' + (role==='user'?'user':'bot') + (text==null?' pending':'');
    if(turn!=null) t.dataset.turn = turn;
    const b = document.createElement('div'); b.className='bubble';
    if(text==null) b.innerHTML = '<span class="dots"></span>';
    else b.textContent = text;
    t.appendChild(b); chat.appendChild(t);
    chat.scrollTop = chat.scrollHeight;
    return t;
  }
  function fillBubble(turn, text){
    const t = chat.querySelector('.turn[data-turn="'+turn+'"]');
    if(t){ t.classList.remove('pending'); t.querySelector('.bubble').textContent = text; }
    else addBubble('bot', text);
    chat.scrollTop = chat.scrollHeight;
    state.turns.push({ role:'assistant', text }); save();
  }

  function run(q){
    if(!started){ const e=document.getElementById('empty'); if(e) e.remove(); started=true; setCollapsed(true); }
    addBubble('user', q);
    state.turns.push({ role:'user', text:q }); save();
    const id = ++turnId;
    addBubble('bot', null, id);
    ask.value=''; ask.style.height='auto';
    vscode.postMessage({ type:'ask', query:q, turn:id });
  }

  function send(){ const q=ask.value.trim(); if(q) run(q); }
  document.getElementById('send').onclick = send;
  ask.addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } });
  ask.addEventListener('input', ()=>{ ask.style.height='auto'; ask.style.height = Math.min(ask.scrollHeight,120)+'px'; });
  document.getElementById('graphBtn').onclick = ()=> vscode.postMessage({ type:'graph' });
  document.getElementById('clear').onclick = ()=>{
    chat.innerHTML=''; state.turns=[]; started=false; save();
    vscode.postMessage({ type:'clear' });
    const e=document.createElement('div'); e.id='empty'; e.textContent='Conversation cleared. Ask Lore anything about this codebase.'; chat.appendChild(e);
  };

  window.addEventListener('message', e=>{
    const m=e.data;
    if(m.type==='context') renderCtx(m.data);
    else if(m.type==='answer') fillBubble(m.turn, m.answer);
    else if(m.type==='run') run(m.query);
  });

  // restore prior conversation (survives reload)
  if(state.turns && state.turns.length){
    const e=document.getElementById('empty'); if(e) e.remove(); started=true;
    for(const t of state.turns) addBubble(t.role==='user'?'user':'bot', t.text);
    setCollapsed(state.collapsed!==false);
  }
  vscode.postMessage({ type:'ready' });
</script></body></html>`;
}

function graphHtml(webview, mem) {
  const n = nonce();
  const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${n}';`;
  const data = JSON.stringify(mem || {});
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  html,body{ margin:0; height:100%; background:#0b0f14; color:#c9d1d9; font-family: var(--vscode-font-family); overflow:hidden; }
  #wrap{ position:relative; width:100%; height:100vh; }
  #title{ position:absolute; top:14px; left:18px; z-index:2; }
  #title h2{ margin:0; font-size:15px; } #title p{ margin:3px 0 0; font-size:12px; opacity:.6; }
  svg{ width:100%; height:100%; display:block; }
  .edge{ stroke:#30363d; stroke-width:1.4; }
  .node circle{ cursor:pointer; transition:r .15s, filter .15s; }
  .node text{ fill:#c9d1d9; font-size:11px; pointer-events:none; }
  .node .sub{ fill:#8b949e; font-size:10px; }
  .pulse{ animation: pulse 1.4s ease-out infinite; }
  @keyframes pulse{ 0%{opacity:.7} 50%{opacity:.18} 100%{opacity:.7} }
  #info{ position:absolute; right:16px; bottom:16px; width:300px; max-width:42%; background:#0d1620; border:1px solid #21323f; border-radius:8px; padding:12px 14px; font-size:12.5px; line-height:1.5; z-index:2; display:none; }
  #info .sha{ color:#58a6ff; font-family: var(--vscode-editor-font-family); } #info .chip{ display:inline-block; font-size:10px; padding:1px 7px; border-radius:10px; background:#3b2326; color:#ff8a80; margin-top:6px; }
  .legend{ position:absolute; left:18px; bottom:16px; font-size:11px; opacity:.7; z-index:2; }
  .legend span{ display:inline-flex; align-items:center; gap:5px; margin-right:14px; }
  .dot{ width:9px; height:9px; border-radius:50%; display:inline-block; }
</style></head><body>
<div id="wrap">
  <div id="title"><h2>🛡 Knowledge Graph</h2><p id="sub"></p></div>
  <svg id="g" viewBox="0 0 1000 680" preserveAspectRatio="xMidYMid meet"></svg>
  <div class="legend">
    <span><i class="dot" style="background:#e5534b"></i> security advisory</span>
    <span><i class="dot" style="background:#388bfd"></i> decision</span>
    <span><i class="dot" style="background:#2ea043"></i> subsystem</span>
  </div>
  <div id="info"></div>
</div>
<script nonce="${n}">
  const MEM = ${data};
  const svg = document.getElementById('g');
  const info = document.getElementById('info');
  document.getElementById('sub').textContent = (MEM.repo||'') + '  —  ' + (MEM.subsystem||'');
  const NS='http://www.w3.org/2000/svg';
  function el(t,a){ const e=document.createElementNS(NS,t); for(const k in (a||{})) e.setAttribute(k,a[k]); return e; }
  function esc(s){ return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

  const W=1000,H=680,cx=W/2,cy=H/2+10;
  const items = (MEM.timeline||[]).slice().sort((a,b)=>(a.date||'').localeCompare(b.date||''));
  // subsystem node in the centre, decisions on an arc around it (oldest -> newest)
  const subN = el('g',{class:'node'});
  const subC = el('circle',{cx:cx,cy:cy,r:30,fill:'#10301c',stroke:'#2ea043','stroke-width':2});
  subN.appendChild(subC);
  const leaf = (MEM.subsystem||'file').split('/').pop();
  const subT = el('text',{x:cx,y:cy+50,'text-anchor':'middle'}); subT.textContent = leaf;
  subN.appendChild(subT);

  const N=items.length, R=235;
  const pts=[];
  items.forEach((it,i)=>{
    const ang = (-Math.PI/2) + (i/Math.max(1,N-1) - 0.5) * Math.PI*1.5;
    const x = cx + R*Math.cos(ang), y = cy + R*Math.sin(ang);
    pts.push({x,y,it});
  });
  // edges first
  pts.forEach(p=> svg.appendChild(el('line',{class:'edge',x1:cx,y1:cy,x2:p.x,y2:p.y})));
  svg.appendChild(subN);
  // decision nodes
  pts.forEach((p,idx)=>{
    const it=p.it; const hasAdv=(it.ghsa||[]).length>0;
    const g=el('g',{class:'node'});
    const c=el('circle',{cx:p.x,cy:p.y,r:hasAdv?17:13, fill: hasAdv?'#3b1d1d':'#10243d', stroke: hasAdv?'#e5534b':'#388bfd','stroke-width':2});
    if(it.at_risk){ const ring=el('circle',{class:'pulse',cx:p.x,cy:p.y,r:24,fill:'none',stroke:'#e5534b','stroke-width':2}); g.appendChild(ring); }
    g.appendChild(c);
    const t=el('text',{x:p.x,y:p.y-(hasAdv?25:21),'text-anchor':'middle'}); t.textContent=it.sha; g.appendChild(t);
    const d=el('text',{class:'sub',x:p.x,y:p.y+(hasAdv?30:26),'text-anchor':'middle'}); d.textContent=it.date; g.appendChild(d);
    c.addEventListener('click',()=>{
      const adv=(it.ghsa||[]).map(x=>'GHSA-'+x).join(', ');
      info.style.display='block';
      info.innerHTML = '<div><span class="sha">'+esc(it.sha)+'</span> &middot; '+esc(it.date)+'</div>'
        + '<div style="margin-top:6px">'+esc(it.message)+'</div>'
        + (adv? '<span class="chip">'+esc(adv)+'</span>':'')
        + (it.at_risk? '<div style="margin-top:8px;color:#ff8a80">⚠ currently flagged at-risk</div>':'');
    });
    // staggered entrance
    g.style.opacity='0'; g.style.transform='scale(.6)'; g.style.transformOrigin=p.x+'px '+p.y+'px';
    g.style.transition='opacity .4s ease '+(idx*0.08)+'s, transform .4s ease '+(idx*0.08)+'s';
    requestAnimationFrame(()=>{ g.style.opacity='1'; g.style.transform='scale(1)'; });
    svg.appendChild(g);
  });
</script></body></html>`;
}

module.exports = { activate, deactivate };
