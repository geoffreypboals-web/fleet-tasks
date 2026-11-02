#!/usr/bin/env python3
"""mcp.py -- the fleet queue as a small MCP server for Claude Code (board WP-152 step 1).

A Claude session hands work to the local machines with these tools instead
of running fleet.py in Bash: submit a task (or a split job), follow it, read
its answer, see what each machine is doing, and see what waits for the
owner's OK. Standard library only, JSON-RPC 2.0 over stdio, one message per
line.

On purpose, nothing here can approve, deny or turn on paid AI: tasks are
submitted with frontier=False, so a task reaches Claude or Gemini only
through the normal gate, which means the owner's OK on the page or with
fleet.py approve. A session may submit at most SUBMITS_PER_HOUR tasks or notes.
Notes can be saved and searched (fleet_remember, fleet_recall) but only
the owner deletes them (the page, or fleet.py forget).

  claude mcp add -s user fleet -- python3 /opt/stacks/fleet-tasks/mcp.py   (install.sh --mcp)
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fleet_web", HERE / "web.py")
web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web)
fleet = web.fleet       # the same fleet module the page uses (web.py loads it)

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SUBMITS_PER_HOUR = 30
PROMPT_MAX_CHARS = 200_000
RESULT_MAX_CHARS = 20_000
WAIT_MAX_S = 300

_submits = []           # this session's submit times, for the hourly cap


def _kind_enum():
    return sorted(fleet.kinds())


def tools():
    """Kept short: every Claude session pays for this text."""
    kinds = _kind_enum()
    return [
        {"name": "fleet_submit",
         "description": "Queue a task for the local AI machines (free). Returns its id. "
                        "Optionally give code: repo (+ paths, or pr) is read into the prompt.",
         "inputSchema": {"type": "object", "required": ["kind", "prompt"], "properties": {
             "kind": {"type": "string", "enum": kinds},
             "prompt": {"type": "string"},
             "tier": {"type": "string", "enum": fleet.TIERS[:-1]},
             "check": {"type": "boolean", "description": "second model checks the answer"},
             "system": {"type": "string"},
             "repo": {"type": "string"},
             "paths": {"type": "array", "items": {"type": "string"}},
             "pr": {"type": "integer"},
             "urls": {"type": "array", "items": {"type": "string"},
                      "description": "public web pages to read (kind research); screened first"}}}},
        {"name": "fleet_split",
         "description": "Split long text across machines, answer each part, merge. Returns the job id.",
         "inputSchema": {"type": "object", "required": ["kind", "prompt", "text"], "properties": {
             "kind": {"type": "string", "enum": kinds},
             "prompt": {"type": "string", "description": "what to do with each part"},
             "text": {"type": "string"},
             "part_chars": {"type": "integer"}}}},
        {"name": "fleet_status",
         "description": "One task (id) with notes and result, or the latest tasks.",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "integer"}, "limit": {"type": "integer"}}}},
        {"name": "fleet_result",
         "description": "Wait up to timeout seconds (default 60, max 300) for a task's answer.",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {
             "id": {"type": "integer"}, "timeout": {"type": "integer"}}}},
        {"name": "fleet_nodes",
         "description": "Each machine (up/busy/down, model, why) and each paid AI: calls used, and any usage lock.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "fleet_waiting",
         "description": "Tasks waiting for the owner's OK to use paid AI. Only the owner can approve.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "fleet_remember",
         "description": "Save a note to the fleet's shared memory (no secrets; refused if it looks like one).",
         "inputSchema": {"type": "object", "required": ["text"], "properties": {
             "text": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
             "source": {"type": "string"}}}},
        {"name": "fleet_recall",
         "description": "Search the shared memory; returns the best-matching notes with their source and date.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string"}, "k": {"type": "integer"},
             "tags": {"type": "array", "items": {"type": "string"}}}}},
    ]


class ToolError(Exception):
    """A tool call that can't be done; shown to the model as an error result."""


def _cut(text, n=None):
    text, n = text or "", n or RESULT_MAX_CHARS
    return text if len(text) <= n else text[:n] + f"\n[... {len(text) - n:,} more characters: fleet.py status ID]"


def _count_submit():
    cutoff = time.time() - 3600
    _submits[:] = [t for t in _submits if t > cutoff]
    if len(_submits) >= SUBMITS_PER_HOUR:
        raise ToolError(f"this session already submitted {SUBMITS_PER_HOUR} tasks in the last hour; wait, or ask the owner")
    _submits.append(time.time())


def _text(args, key, required=True):
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise ToolError(f"{key} must be non-empty text")
    if len(v) > PROMPT_MAX_CHARS:
        raise ToolError(f"{key} is {len(v):,} characters; the most is {PROMPT_MAX_CHARS:,} (use fleet_split)")
    return v


def _mark(tid, how):
    with fleet.conn() as c:
        fleet.note(c, tid, f"submitted by a Claude session through MCP ({how})")


def t_submit(a):
    kind, prompt = _text(a, "kind"), _text(a, "prompt")
    system = _text(a, "system", required=False) or ""
    tier = a.get("tier")
    if tier == "frontier":
        raise ToolError("tasks start on the local machines; paid AI is reached only through the owner's OK")
    _count_submit()
    try:
        if a.get("urls"):
            if a.get("repo") or a.get("paths") or a.get("pr"):
                raise ToolError("give urls or repo, not both")
            urls = [str(u) for u in a["urls"]][:20]
            tid, n, notes = fleet.submit_research(kind, prompt, urls, tier, bool(a.get("check")), False, system)
            _mark(tid, "fleet_submit, web pages")
            return (f"task #{tid} queued" + (f" (split into {n} parts)" if n > 1 else "") + ". Pages:\n  "
                    + "\n  ".join(notes) + "\nFollow it with fleet_result.")
        if a.get("repo"):
            tid, n = fleet.submit_source(kind, prompt, a["repo"], a.get("paths") or (), a.get("pr"),
                                         tier, bool(a.get("check")), False, system)
        elif a.get("paths") or a.get("pr"):
            raise ToolError("paths and pr need repo")
        else:
            tid, n = fleet.submit(kind, prompt, tier, bool(a.get("check")), False, system), 1
    except ValueError as e:
        raise ToolError(str(e))
    _mark(tid, "fleet_submit")
    return f"task #{tid} queued" + (f" (split into {n} parts)" if n > 1 else "") + \
        ". Follow it with fleet_result or fleet_status."


def t_split(a):
    kind, prompt = _text(a, "kind"), _text(a, "prompt")
    text = a.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("text must be non-empty")
    if len(text) > 5 * PROMPT_MAX_CHARS:
        raise ToolError(f"text is {len(text):,} characters; the most is {5 * PROMPT_MAX_CHARS:,}")
    _count_submit()
    parts = fleet.split_text(text, None, int(a.get("part_chars") or 6000))
    try:
        pid = fleet.submit_split(kind, prompt, parts, None, False, False, "")
    except ValueError as e:
        raise ToolError(str(e))
    _mark(pid, "fleet_split")
    return f"job #{pid} queued, split into {max(1, len(parts))} parts. Follow it with fleet_result."


def _row(c, tid):
    r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        raise ToolError(f"no task #{tid}")
    return r


def _words(status):
    return web.STATUS_WORDS.get(status, status)


def t_status(a):
    with fleet.conn() as c:
        if a.get("id") is not None:
            r = _row(c, int(a["id"]))
            head = (f"#{r['id']} {r['kind']}: {_words(r['status'])} (tier {r['tier']}"
                    + (f", answered by {r['by']}" if r["by"] else "") + (f", source {r['source']}" if r["source"] else "") + ")"
                    + (f"\nrouted: {r['routed']}" if _has(r, "routed") and r["routed"] else ""))
            notes = "\n".join(r["notes"].strip().splitlines()[-8:])
            return _cut(f"{head}\nnotes:\n{notes}\n\nresult:\n{r['result'] or '(none yet)'}")
        n = max(1, min(int(a.get("limit") or 15), 50))
        rows = c.execute("SELECT id, kind, status, tier, by FROM tasks ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return "\n".join(f"#{r['id']} {r['kind']} {_words(r['status'])} tier={r['tier']}" + (f" by={r['by']}" if r["by"] else "")
                     for r in rows) or "the queue is empty"


def t_result(a):
    if a.get("id") is None:
        raise ToolError("id is required")
    tid = int(a["id"])
    wait = max(0, min(int(a.get("timeout") if a.get("timeout") is not None else 60), WAIT_MAX_S))
    end = time.time() + wait
    with fleet.conn() as c:
        while True:
            r = _row(c, tid)
            if r["status"] == "done":
                return _cut(f"#{tid} done (answered by {r['by'] or '?'}):\n\n{r['result']}")
            if r["status"] in fleet.FINAL:
                last = (r["notes"].strip().splitlines() or [""])[-1]
                return f"#{tid} ended: {_words(r['status'])}. {last}"
            if r["status"] == "needs_approval":
                return (f"#{tid} waits for the owner's OK to use paid AI. Only the owner can approve "
                        f"({fleet.PAGE_URL}).")
            if time.time() >= end:
                return f"#{tid} is still {_words(r['status'])}; ask again with fleet_result."
            time.sleep(2)


def _has(row, col):
    try:
        return col in row.keys()
    except AttributeError:
        return col in row


def t_nodes(a):
    s = web.state()
    lines = [f"{m['name']} ({m['tier']}, {m['model']}): {m['state']}, {m['why']}" for m in s["machines"]]
    for p in s["providers"]:
        lines.append(f"paid AI {p['name']}: {'on' if p['enabled'] else 'off'}, {fleet.paid_line(p)}")
    lines.append(f"paid AI total: {s['total']['used']}/{s['total']['limit']} calls today")
    if s["paused"]:
        lines.append("queue is PAUSED: no new work starts")
    if s["stop"]:
        lines.append("STOP switch is on: no paid AI")
    return "\n".join(lines)


def t_waiting(a):
    with fleet.conn() as c:
        rows = c.execute("SELECT id, kind, substr(prompt, 1, 120) AS preview, notes FROM tasks "
                         "WHERE status='needs_approval' ORDER BY id").fetchall()
    if not rows:
        return "nothing waits for the owner's OK"
    out = [f"#{r['id']} {r['kind']}: {r['preview'].strip()!r}. "
           f"{(r['notes'].strip().splitlines() or [''])[-1]}" for r in rows]
    return "\n".join(out) + f"\nOnly the owner can approve or deny, on the page ({fleet.PAGE_URL}) or with fleet.py approve ID."


def t_remember(a):
    text = _text(a, "text")
    if len(text) > fleet.NOTE_MAX_CHARS:
        raise ToolError(f"the note is {len(text):,} characters; the most is {fleet.NOTE_MAX_CHARS:,}")
    _count_submit()
    tags = [str(t) for t in (a.get("tags") or [])][:10]
    try:
        nid, vec = fleet.remember(text, "claude session (MCP)" + (f": {a['source']}" if a.get("source") else ""), tags)
    except ValueError as e:
        raise ToolError(str(e))
    return f"note {nid} saved" + ("" if vec else " (indexed later; searchable by words until then)")


def t_recall(a):
    q = _text(a, "query")
    try:
        hits = fleet.recall(q, max(1, min(int(a.get("k") or 5), 20)), [str(t) for t in (a.get("tags") or [])])
    except ValueError as e:
        raise ToolError(str(e))
    if not hits:
        return "nothing in memory matches"
    return _cut("\n\n".join(f"[{h['id']}] {h['score']:.2f} by {h['how']}, {h['created']}, {h['source'] or 'no source'}"
                             + (f", tags {','.join(h['tags'])}" if h["tags"] else "") + f"\n{h['text']}" for h in hits))


CALLS = {"fleet_submit": t_submit, "fleet_split": t_split, "fleet_status": t_status,
         "fleet_result": t_result, "fleet_nodes": t_nodes, "fleet_waiting": t_waiting,
         "fleet_remember": t_remember, "fleet_recall": t_recall}


def handle(msg):
    """One JSON-RPC message in, one reply out (None for a notification)."""
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:
        return None                                     # notifications/initialized and the like
    try:
        if method == "initialize":
            want = (msg.get("params") or {}).get("protocolVersion")
            res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "fleet", "version": "1.0"},
                   "instructions": "Local AI machines on the owner's tailnet. Hand summarising, drafting, "
                                   "classifying and checking here first; it costs no paid AI."}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": tools()}
        elif method == "tools/call":
            p = msg.get("params") or {}
            fn = CALLS.get(p.get("name"))
            if not fn:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {p.get('name')!r}"}}
            try:
                res = {"content": [{"type": "text", "text": fn(p.get("arguments") or {})}]}
            except ToolError as e:
                res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    except Exception as e:                              # never crash the session's server
        res = {"content": [{"type": "text", "text": f"fleet error: {type(e).__name__}: {e}"}], "isError": True}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
