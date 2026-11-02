#!/usr/bin/env python3
"""web.py -- the fleet-tasks page (board WP-131): one screen that shows what
every AI machine on the tailnet is doing and why, the queue, what's waiting
for the owner's OK, and today's frontier budget, with approve/deny, submit
and a stop-all switch. Help is built in: every control has a tooltip and the
"How to use" panel has worked examples.

Standard library only. Listens on 127.0.0.1:8960 and is reached through
`tailscale serve` (https://gpu-server.<tailnet>.ts.net:8960), which adds the
Tailscale-User-Login header: only logins in FLEET_ALLOWED_LOGINS get in, the
same rule as Family Finance's tailnet_identity.decide(). Changes need a
POST with the X-Fleet header, which another site's page can't send.

Without Tailscale: FLEET_LOCAL_LOGIN=<a name> lets a request that comes from
this machine with no Tailscale header in, as that name (open the page through
`ssh -L 8960:127.0.0.1:8960 <server>`). Anyone who can log in to this machine
can then open it. Don't also run `tailscale serve` for the page: a tagged
tailnet device's request reaches the page without a login header.

  web.py [--port 8960]        (fleet-web.service)
"""
import argparse
import base64
import binascii
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fleet", HERE / "fleet.py")
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)
_dspec = importlib.util.spec_from_file_location("fleet_doc", HERE / "doc.py")
DOC = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(DOC)
DESIGN = fleet.design_mod()
_bkspec = importlib.util.spec_from_file_location("fleet_backlog", HERE / "backlog.py")
BACKLOG_MOD = importlib.util.module_from_spec(_bkspec)
_bkspec.loader.exec_module(BACKLOG_MOD)
BACKLOG = BACKLOG_MOD.Backlog(fleet)     # the Backlog tab, builds and autopilot (D-2026-10-31-05)
WP_ID = re.compile(r"[A-Za-z]+-\d+[A-Za-z0-9-]*")

TRUSTED = {"127.0.0.1", "::1"}
MAX_BODY = 200_000
VOICE = os.environ.get("FLEET_VOICE_URL", "http://127.0.0.1:8961")   # voice/voice.py: speech to text, local
MAX_RECORDING = 15_000_000
MAX_DOC_BODY = 14_000_000       # a 10 MB document, base64 in JSON
MAX_PROMPT = 100_000    # characters; bigger jobs go through the terminal (fleet.py submit @file)
MAX_PATHS = 5000        # paths offered for one checkout in the page's path list
STATUS_WORDS = {
    "queued": "waiting for a free machine", "claimed": "being worked on", "done": "finished",
    "failed": "gave up", "needs_approval": "waiting for your OK", "approved": "approved, about to run",
    "denied": "you said no", "blocked": "stopped by a gate", "expired": "no answer in time",
    "cancelled": "you cancelled it", "split": "split into parts",
}


def minutes_ago(stamp):
    """Whole minutes since an ISO time from the queue (local time), or None."""
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(stamp)).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def allowed_logins():
    """FLEET_ALLOWED_LOGINS, comma separated, lowercased."""
    return {x.strip().lower() for x in os.environ.get("FLEET_ALLOWED_LOGINS", "").split(",") if x.strip()}


def local_login():
    """FLEET_LOCAL_LOGIN, lowercased, or "" (off): the name a request from this
    machine without a Tailscale header gets (no-Tailscale installs)."""
    return os.environ.get("FLEET_LOCAL_LOGIN", "").strip().lower()


def decide(login, peer, allowed, local=""):
    """(True, login) or (False, reason). Same rule as Family Finance's
    tailnet_identity.decide() in required mode, plus the opt-in local login:
    with `local` set, a request from this machine without a login header gets
    in as `local`."""
    if login and peer in TRUSTED:
        if login.lower() in allowed:
            return True, login.lower()
        return False, "This tailnet user isn't allowed here."
    if local and not login and peer in TRUSTED:
        return True, local
    return False, "Open this page through its tailnet address (tailscale serve)."


def claude_turns_today():
    """Claude turns counted today by the optional Stop hook (hooks/claude_hook.py), or None."""
    path = fleet.STATE / "claude-usage.jsonl"
    if not path.exists():
        return None
    day = '{"at": "' + datetime.now().date().isoformat()
    with open(path) as f:
        return sum(line.startswith(day) for line in f)


USAGE = fleet.usage_mod()
_usage_lock = threading.Lock()


def plan_usage():
    """The last usage.py snapshot (no AI asked). Older than 15 minutes: read again
    on a background thread, so a page refresh never waits on GitHub or the logs."""
    try:
        u = json.loads((fleet.STATE / "usage.json").read_text())
    except (OSError, ValueError):
        u = {}
    if time.time() - u.get("t", 0) >= USAGE.MAX_AGE and _usage_lock.acquire(blocking=False):
        def read():
            try:
                USAGE.snapshot(refresh=True)
            finally:
                _usage_lock.release()
        threading.Thread(target=read, daemon=True).start()
    return {k: u[k] for k in ("at", "copilot", "claude") if k in u} or None


PAGE_ROWS = 40                          # queue rows per page; older pages via ?page=N
WATCH_ROWS = 20                         # job-watch checks per page (?wpage=N)
QUIET_KINDS = ("watch",)                # routine checks: their own section, so they don't bury the owner's jobs


def state(page=0, wpage=0):
    """Everything the page shows, in one JSON object. page 0 is the newest
    jobs; each later page is older. wpage pages the job-watch checks."""
    p = fleet.policy()
    with fleet.conn() as c:
        return _state(p, c, max(0, int(page)), max(0, int(wpage)))


def _paged(c, where, args, page, rows):
    """(rows on this page, page, pages, matching count), newest first; a page past the end shows the last."""
    n = c.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", args).fetchone()[0]
    pages = max(1, -(-n // rows))
    page = min(page, pages - 1)
    found = [dict(r) for r in c.execute(f"SELECT {TASK_COLS}, substr(result, 1, 600) AS result_head FROM tasks "
                                        f"WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, rows, page * rows))]
    return found, page, pages, n


def earlier_list(c, n=40):
    """Recent finished jobs, for the Ask form's "Use earlier work" picker."""
    q = ",".join("?" * len(QUIET_KINDS))
    return [{"id": r["id"], "ask": re.sub(r"\s+", " ", DOC.ask_of(r["prompt"]))[:90]} for r in c.execute(
        f"SELECT id, substr(prompt, 1, 2000) AS prompt FROM tasks WHERE parent_id=0 AND status='done' AND kind NOT IN ({q}) "
        "ORDER BY id DESC LIMIT ?", (*QUIET_KINDS, n))]


def watch_view(c, wpage):
    """job-watch's checks: which job, its verdict and note, when; and how many said there's a problem today."""
    q = ",".join("?" * len(QUIET_KINDS))
    found, wpage, pages, n = _paged(c, f"parent_id=0 AND kind IN ({q})", QUIET_KINDS, wpage, WATCH_ROWS)
    checks = []
    for t in found:
        job = re.match(r"Job: (.*)", t["preview"] or "")
        try:
            verdict = json.loads(re.search(r"\{.*\}", t["result_head"] or "", re.DOTALL).group(0))
        except (AttributeError, ValueError):
            verdict = {}
        checks.append({"id": t["id"], "job": job.group(1).strip() if job else (t["preview"] or "")[:60],
                       "status": t["status"], "status_words": STATUS_WORDS.get(t["status"], t["status"]),
                       "verdict": str(verdict.get("status", "")) if t["status"] == "done" else "",
                       "note": str(verdict.get("note", ""))[:300], "updated": t["updated"], "by": t["by"]})
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    problems = sum(1 for (r,) in c.execute(f"SELECT substr(result, 1, 600) FROM tasks WHERE parent_id=0 AND kind IN ({q}) "
                                           "AND status='done' AND updated >= ?", (*QUIET_KINDS, day))
                   if not re.search(r'"status"\s*:\s*"ok"', r or ""))
    return {"checks": checks, "page": wpage, "pages": pages, "count": n, "rows": WATCH_ROWS, "problems_today": problems}


TASK_COLS = ("id, kind, status, tier, by, worker, attempts, updated, escalations, escalated_why, parent_id, part, source, uses, only, "
             "substr(prompt, 1, 160) AS preview, substr(notes, -400) AS notes_tail")


def _state(p, c, page=0, wpage=0):
    working = {r["worker"]: dict(r) for r in c.execute(
        "SELECT id, kind, worker, updated, substr(partial, -400) AS preview FROM tasks "
        "WHERE status='claimed' AND worker != ''")}
    last = {}                           # each machine's most recent finished task
    for r in c.execute("SELECT id, worker, status, updated FROM tasks WHERE worker != '' AND status != 'claimed' "
                       "ORDER BY updated DESC LIMIT 500"):
        last.setdefault(r["worker"], dict(r))
    machines = []
    for e in fleet.endpoints():
        if not e["up"]:
            st, why = "down", "not answering (asleep, off, or Ollama isn't serving on the tailnet)"
        elif fleet.free(e):
            st, why = "free", "idle, or already holds its model"
        elif not fleet.gpu_ok(e)[0]:
            st, why = "busy", fleet.gpu_ok(e)[1]
        else:
            st, why = "busy", "another job's model is loaded (" + ", ".join(e["loaded"]) + "); it waits, never evicts"
        job = working.get(e["name"])
        if job:
            st, why = "working", f"task #{job['id']} ({job['kind']})"
        prev = last.get(e["name"])
        machines.append({"name": e["name"], "tier": e["tier"], "model": e["model"], "state": st, "why": why,
                         "job": {"id": job["id"], "preview": job["preview"] or ""} if job else None,
                         "loaded": e["loaded"],
                         "last": {"id": prev["id"], "status": prev["status"], "ago_min": minutes_ago(prev["updated"])}
                         if prev else None})
    entries = fleet.read_ledger()       # read once per refresh
    providers = fleet.providers_state(p, entries)
    total = {"used": fleet.calls_today(None, entries), "limit": fleet.daily_total(p)}
    q = ",".join("?" * len(QUIET_KINDS))
    tasks, page, pages, jobs = _paged(c, f"parent_id=0 AND kind NOT IN ({q})", QUIET_KINDS, page, PAGE_ROWS)
    for t in tasks:
        t.pop("result_head")
    # a split job is one row: its parts and merges are inside it (the task window lists them)
    ids = ",".join(str(int(t["id"])) for t in tasks) or "0"
    progress = {}
    for r in c.execute(f"SELECT parent_id, part / {fleet.MERGE_ROUND} AS rnd, COUNT(*) AS total, "
                       f"SUM(status='done') AS done FROM tasks WHERE parent_id IN ({ids}) GROUP BY parent_id, rnd"):
        progress.setdefault(r["parent_id"], {})[r["rnd"]] = (r["done"], r["total"])
    for t in tasks:
        t["status_words"] = STATUS_WORDS.get(t["status"], t["status"])
        t["parts"] = None
        if t["id"] in progress:
            rounds = progress[t["id"]]
            done, total = rounds.get(0, (0, 0))
            last = max(rounds)
            t["parts"] = {"done": done, "total": total, "round": last,
                          "round_done": rounds[last][0], "round_total": rounds[last][1]}
        t["last_note"] = (t.pop("notes_tail") or "").strip().splitlines()[-1:] or [""]
        t["last_note"] = re.sub(r"^\[[^]]*\] ", "", t["last_note"][0])     # drop the time stamp; the row has one
    kinds = [{"name": k, "tier": v.get("tier", "small"), "frontier": v.get("frontier", [])}
             for k, v in fleet.kinds().items()]
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    return {"machines": machines, "providers": providers, "total": total, "claude_turns": claude_turns_today(), "usage": plan_usage(),
            "screen": fleet.screen_recent(8), "stop": (fleet.STATE / "STOP").exists(),
            "paused": fleet.paused(), "cancellable": list(fleet.CANCELLABLE),
            "tasks": tasks, "queue_page": {"page": page, "pages": pages, "jobs": jobs, "rows": PAGE_ROWS},
            "watch": watch_view(c, wpage), "kinds": kinds, "earlier": earlier_list(c), "followup_kind": p.get("followup_kind", "draft"), "counts": counts, "ledger": fleet.ledger_tail(15, entries),
            "waiting": waiting(c)}


def waiting(c):
    """Every task waiting for the owner's OK (on any queue page), with the paid
    model that would answer."""
    out = [dict(r) for r in c.execute(f"SELECT {TASK_COLS} FROM tasks WHERE status='needs_approval' ORDER BY id DESC")]
    for t in out:
        t["status_words"] = STATUS_WORDS.get(t["status"], t["status"])
        t["last_note"] = re.sub(r"^\[[^]]*\] ", "", ((t.pop("notes_tail") or "").strip().splitlines()[-1:] or [""])[0])
    for t in out:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone()
        try:
            t["would_use"] = fleet.would_use(row) if row else ""
        except Exception as e:                  # the page must still load
            t["would_use"] = f"(can't tell: {e.__class__.__name__})"
    return out


def models_view():
    """The Models table: each model's accepted fit per kind (with our pass
    rate), and the proposals waiting for the owner."""
    cat = fleet.CATALOG.load()
    data, st = fleet.CATALOG.scores(fleet.STATE), fleet.CATALOG.stats(fleet.STATE)
    acc = fleet.CATALOG.accepted(data)
    rows = []
    for m in cat.get("models", []):
        fits = {}
        for k in cat.get("kinds", []):
            f, why = fleet.CATALOG.fit(m["name"], k, acc, st)
            if f is not None:
                n, rate = st.get((m["name"], k), (0, 0))
                fits[k] = {"fit": f, "review": acc[(m["name"], k)]["score"], "why": why, "ours": round(rate * 100) if n else None, "n": n}
        rows.append({"name": m["name"], "cost": m.get("cost", ""), "weight": m.get("weight", 1),
                     "where": ", ".join(m.get("endpoints", [])) or m.get("provider", ""), "fits": fits})
    proposed = [it for it in data["items"] if it["status"] == "proposed"]
    return {"kinds": cat.get("kinds", []), "models": rows, "proposed": proposed}


def task_detail(tid):
    """One task: prompt, notes, result."""
    with fleet.conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["status_words"] = STATUS_WORDS.get(d["status"], d["status"])
    d["ask"] = DOC.ask_of(d["prompt"])
    d["result_html"] = DOC.to_html(d["result"]) if d["result"] else ""
    d["built_page"] = bool(DOC.built_page(d["result"]))
    d["suggestions"] = DOC.suggestions(d["result"]) if d["status"] == "done" else []
    d["route_options"] = (fleet.route_options(r) if d["tier"] == "frontier" and d["status"] in fleet.NOT_STARTED
                          else [])
    with fleet.conn() as c:
        d["parts"] = [dict(x, status_words=STATUS_WORDS.get(x["status"], x["status"])) for x in c.execute(
            "SELECT id, part, status, by FROM tasks WHERE parent_id=? ORDER BY part", (tid,))]
        d["followups"] = [dict(x, status_words=STATUS_WORDS.get(x["status"], x["status"]), ask=DOC.ask_of(x["prompt"])[:120])
                          for x in c.execute("SELECT id, status, prompt FROM tasks WHERE instr(',' || uses || ',', ?) > 0 "
                                             "ORDER BY id", (f",{tid},",))]
        for x in d["followups"]:
            x.pop("prompt")
    return d


def voice_ok():
    try:
        with urllib.request.urlopen(VOICE + "/health", timeout=2) as r:
            return bool(json.load(r).get("ok"))
    except (OSError, ValueError):
        return False


def listen(data):
    """(status, body): the words in a recording, from the local voice service."""
    req = urllib.request.Request(VOICE + "/listen", data=data, headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return 200, {"text": str(json.load(r).get("text", ""))}
    except urllib.error.HTTPError as e:
        try:
            why = json.load(e).get("error", "")
        except ValueError:
            why = ""
        return 400, {"error": f"Couldn't hear that: {why or e.code}."}
    except (OSError, ValueError):
        return 503, {"error": "Voice isn't running on gpu-server (fleet-voice service)."}


class Handler(BaseHTTPRequestHandler):
    """GET / (page), /api/backlog (the board's open work, backlog.py), /doc/N (a task as a document page), /doc/N.docx (as a Word file), /api/state[?page=N&wpage=N], /api/task/N, /api/repos[/NAME], /api/memory[?q=], /api/voice, /health;
    POST /api/backlog/(order|assign/WP|plan/WP|build/WP|answer/WP|approve/N|deny/N|autopilot) (backlog.py),
    POST /api/listen (a recording -> its words), /api/submit, /api/document (a file, base64), /api/followup/N, /api/budget/raise, /api/only/N (limit a task to some paid AIs), /api/paid-reset (a paid AI's fresh start on a kind), /api/design/start, /api/design/N/(approve|change|manual|cancel), /api/approve/N, /api/deny/N, /api/cancel/N, /api/stop, /api/pause,
    /api/memory/delete/N, /api/unlock/NAME, /api/scores/accept/N, /api/scores/reject/N."""

    def log_message(self, fmt, *args):  # quiet: the page polls every few seconds
        pass

    def _send(self, code, body, kind="application/json", csp=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data, kind):
        """An image shown on the page (a design's preview)."""
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, data, kind, name):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _who(self):
        """The allowed login, or None after sending a 403."""
        ok, who = decide(self.headers.get("Tailscale-User-Login"), self.client_address[0], allowed_logins(),
                         local_login())
        if not ok:
            self._send(403, {"error": who})
            return None
        return who

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"ok": True})
        if not self._who():
            return
        if path == "/":
            return self._send(200, PAGE, "text/html")
        if path == "/api/state":
            qs = parse_qs(urlparse(self.path).query)
            num = lambda k: int(qs[k][0]) if qs.get(k, [""])[0].isdigit() else 0
            return self._send(200, state(num("page"), num("wpage")))
        if path == "/api/backlog":
            try:
                return self._send(200, BACKLOG.state())
            except (BACKLOG_MOD.BOARD.BoardError, OSError, ValueError) as e:
                return self._send(502, {"error": f"Couldn't read the AI board: {e}"})
        if path == "/api/voice":
            return self._send(200, {"ok": voice_ok()})
        m = re.fullmatch(r"/doc/(\d+)(\.docx|/page)?", path)
        if m:
            d = task_detail(int(m.group(1)))
            if not d:
                return self._send(404, {"error": "no such task"})
            if m.group(2) == "/page":
                built = DOC.built_page(d["result"])
                if not built:
                    return self._send(404, {"error": "this answer didn't build a web page"})
                return self._send(200, built, "text/html", csp=DOC.BUILT_CSP)
            if m.group(2):
                return self._send_file(DOC.docx(d), "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                       f"fleet-task-{d['id']}.docx")
            return self._send(200, DOC.page(d), "text/html", csp=DOC.CSP)
        if path == "/api/designs":
            return self._send(200, {"designs": DESIGN.listing()})
        m = re.fullmatch(r"/api/design/(\d+)(?:/(render\.png|render-top\.png|render-bottom\.png|part\.stl|manual\.md|files\.zip))?", path)
        if m:
            did = int(m.group(1))
            if m.group(2) == "files.zip":
                z = DESIGN.zip_of(did)
                if not z:
                    return self._send(404, {"error": "nothing kept yet: approve a part first"})
                return self._send_file(z[1], "application/zip", z[0])
            if not m.group(2):
                d = DESIGN.detail(did)
                if d:
                    d["manual_text"] = DESIGN.manual_text(did)
                return self._send(200 if d else 404, d or {"error": "no such design"})
            f = fleet.STATE / "designs" / str(did) / m.group(2)
            if not f.exists():
                return self._send(404, {"error": "not made yet"})
            if m.group(2).endswith(".png"):
                return self._send_bytes(f.read_bytes(), "image/png")
            kind = "model/stl" if m.group(2) == "part.stl" else "text/markdown; charset=utf-8"
            return self._send_file(f.read_bytes(), kind, f"design-{did}-{m.group(2)}")
        if path.startswith("/api/task/") and path.rsplit("/", 1)[1].isdigit():
            d = task_detail(int(path.rsplit("/", 1)[1]))
            return self._send(200 if d else 404, d or {"error": "no such task"})
        if path == "/api/repos":
            return self._send(200, {"repos": list(fleet.repos())})
        if path == "/api/memory":
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].strip()
            c = fleet.mem()
            try:
                counts = fleet.MEMORY.count(c)
                notes = None if q else fleet.MEMORY.latest(c, 20)
            finally:
                c.close()
            if q:
                notes = fleet.recall(q[:2000], 10)
            return self._send(200, {"notes": notes, "query": q, "total": counts["notes"],
                                    "waiting_for_vector": counts["waiting_for_vector"]})
        if path == "/api/models":
            return self._send(200, models_view())
        if path.startswith("/api/repos/"):
            try:
                files = fleet.repo_files(path.rsplit("/", 1)[1])
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            dirs = sorted({f.rsplit("/", 1)[0] + "/" for f in files if "/" in f})
            return self._send(200, {"paths": (dirs + files)[:MAX_PATHS], "total": len(dirs) + len(files)})
        self._send(404, {"error": "not found"})

    def _backlog_post(self, action, body, login):
        """POST /api/backlog/order {ranks,within,labels,repos} | assign/WP {to} | plan/WP |
        build/WP {builder?} | answer/WP {answer: done|approved|denied, comment} (an owner task) |
        approve/N | deny/N | autopilot {on}. Board refusals come back as 409."""
        if not isinstance(body, dict):
            return self._send(400, {"error": "send a JSON object"})
        parts = action.split("/")
        verb, arg = parts[0], parts[1] if len(parts) > 1 else ""
        try:
            if verb == "order":
                return self._send(200, {"ok": BACKLOG.set_order(body)})
            if verb == "autopilot":
                BACKLOG.set_autopilot(bool(body.get("on")))
                return self._send(200, {"ok": True, "on": bool(body.get("on"))})
            if verb in ("approve", "deny") and arg.isdigit():
                BACKLOG.approve_build(int(arg), verb == "approve")
                return self._send(200, {"ok": True})
            if not WP_ID.fullmatch(arg):
                return self._send(400, {"error": "Which board item? e.g. WP-27"})
            if verb == "answer":
                return self._send(200, {"ok": BACKLOG.owner_answer(arg, str(body.get("answer") or ""),
                                                                   str(body.get("comment") or "")[:2000])})
            if verb == "assign":
                return self._send(200, {"ok": BACKLOG.assign(arg, str(body.get("to") or ""))})
            if verb == "plan":
                return self._send(200, {"id": BACKLOG.plan(arg)})
            if verb == "build":
                builder = str(body.get("builder") or "") or None
                return self._send(200, {"build": BACKLOG.request_build(arg, builder, by=login)})
        except BACKLOG_MOD.BOARD.BoardError as e:
            return self._send(409, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "unknown backlog action"})

    def do_POST(self):
        path = urlparse(self.path).path
        login = self._who()
        if not login:
            return
        if self.headers.get("X-Fleet") != "1":
            return self._send(403, {"error": "missing X-Fleet header"})
        size = int(self.headers.get("Content-Length") or 0)
        if path == "/api/document":
            if not 0 < size <= MAX_DOC_BODY:
                return self._send(413, {"error": "The document is over 10 MB. Attach a smaller part of it."})
            try:
                body = json.loads(self.rfile.read(size))
                name = os.path.basename(str(body.get("name") or "document.txt"))[:120]
                data = base64.b64decode(str(body.get("data") or ""), validate=True)
                kind = str(body.get("kind") or "summarize")
                if kind not in fleet.kinds():
                    return self._send(400, {"error": "Pick one of the kinds in the list."})
                text = fleet.document_text(name, data)
                tid, notes = fleet.submit_document(kind, str(body.get("prompt") or ""), name, text,
                                                   check=bool(body.get("check")), frontier=bool(body.get("frontier")))
            except (ValueError, binascii.Error) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"id": tid, "notes": notes})
        if path == "/api/listen":
            if not 0 < size <= MAX_RECORDING:
                return self._send(413, {"error": "The recording is empty or too long. Keep it under 3 minutes."})
            code, body = listen(self.rfile.read(size))
            return self._send(code, body)
        if size > MAX_BODY:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self._send(400, {"error": "send JSON"})
        if path == "/api/design/start" or re.fullmatch(r"/api/design/\d+/(approve|change|manual|cancel)", path):
            try:
                if path == "/api/design/start":
                    tid = int(body.get("task") or 0)
                    return self._send(200, {"id": DESIGN.start(tid, str(body.get("focus") or "")[:300], by=login)})
                did, verb = int(path.split("/")[3]), path.rsplit("/", 1)[1]
                if verb == "approve":
                    DESIGN.approve(did, by=login)
                elif verb == "change":
                    DESIGN.change(did, str(body.get("words") or ""), by=login)
                elif verb == "manual":
                    DESIGN.want_manual(did, by=login)
                elif not DESIGN.cancel(did, by=login):
                    return self._send(409, {"error": "That design is already over."})
                return self._send(200, {"ok": True})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
        if path.startswith("/api/backlog/"):
            return self._backlog_post(path[len("/api/backlog/"):], body, login)
        if path == "/api/submit":
            prompt = str(body.get("prompt") or "").strip()
            kind = str(body.get("kind") or "summarize")
            if not prompt:
                return self._send(400, {"error": "Write what you want done."})
            if len(prompt) > MAX_PROMPT:
                return self._send(400, {"error": f"That's over {MAX_PROMPT:,} characters. Send big jobs from the terminal."})
            if kind not in fleet.kinds():
                return self._send(400, {"error": "Pick one of the kinds in the list."})
            text = str(body.get("split_text") or "").strip()
            if len(text) > MAX_PROMPT:
                return self._send(400, {"error": f"The text to split is over {MAX_PROMPT:,} characters. Send it from the terminal."})
            repo = str(body.get("repo") or "").strip()
            raw = body.get("paths") or ""
            if isinstance(raw, list):           # a JSON list keeps each path whole, commas included
                paths = [str(x).strip() for x in raw if str(x).strip()]
            else:                               # the page's text box: one per line, or comma separated
                paths = [x.strip() for x in re.split(r"[,\n]", str(raw)) if x.strip()]
            pr = str(body.get("pr") or "").strip()
            raw_urls = body.get("urls") or ""
            urls = [str(x).strip() for x in raw_urls] if isinstance(raw_urls, list) else \
                [x.strip() for x in re.split(r"[\s,]+", str(raw_urls)) if x.strip()]
            raw_use = body.get("use") or ""
            use = [int(x) for x in (raw_use if isinstance(raw_use, list) else re.findall(r"\d+", str(raw_use)))
                   if str(x).isdigit()]
            if use and (urls or repo or text or paths or pr):
                return self._send(400, {"error": "Earlier work goes with a plain question. To add pages, code or text, "
                                                 "send that first, then follow up on its answer."})
            if urls and (repo or text or paths or pr):
                return self._send(400, {"error": "Pick web pages, code to read, or text to split: one of them."})
            if (paths or pr) and not repo:
                return self._send(400, {"error": "Pick the checkout the files or PR are in."})
            if pr and not pr.isdigit():
                return self._send(400, {"error": "The PR is its number, like 122."})
            if repo and text:
                return self._send(400, {"error": "Pick code to read or paste text to split, not both."})
            try:
                if urls:
                    tid, _, notes = fleet.submit_research(kind, prompt, urls, check=bool(body.get("check")),
                                                          frontier=bool(body.get("frontier")))
                    return self._send(200, {"id": tid, "notes": notes})
                if repo:
                    tid, _ = fleet.submit_source(kind, prompt, repo, paths, int(pr) if pr else None,
                                                 check=bool(body.get("check")), frontier=bool(body.get("frontier")))
                elif text:
                    tid = fleet.submit_split(kind, prompt, fleet.split_text(text), check=bool(body.get("check")),
                                             frontier=bool(body.get("frontier")))
                else:
                    ids, ref_notes = fleet.earlier_work(prompt, use)
                    if ids:
                        tid = fleet.submit_followup(ids, prompt, kind, bool(body.get("check")), bool(body.get("frontier")))
                        return self._send(200, {"id": tid, "notes": [f"builds on {', '.join('#%d' % i for i in ids)}"] + ref_notes})
                    tid = fleet.submit(kind, prompt, check=bool(body.get("check")), frontier=bool(body.get("frontier")))
                    if ref_notes:
                        return self._send(200, {"id": tid, "notes": ref_notes})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"id": tid})
        if path.startswith("/api/followup/") and path.rsplit("/", 1)[1].isdigit():
            kind = str(body.get("kind") or "") or None
            if kind and kind not in fleet.kinds():
                return self._send(400, {"error": "Pick one of the kinds in the list."})
            if len(str(body.get("prompt") or "")) > MAX_PROMPT:
                return self._send(400, {"error": f"That's over {MAX_PROMPT:,} characters."})
            try:
                ids, _ = fleet.earlier_work(str(body.get("prompt") or ""), [int(path.rsplit("/", 1)[1])])
                tid = fleet.submit_followup(ids, str(body.get("prompt") or ""), kind,
                                            bool(body.get("check")), bool(body.get("frontier")))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"id": tid})
        for verb in ("approve", "deny"):
            if path.startswith(f"/api/{verb}/") and path.rsplit("/", 1)[1].isdigit():
                with fleet.conn() as c:
                    ok = fleet.decide(c, int(path.rsplit("/", 1)[1]), verb == "approve", by=login)
                return self._send(200 if ok else 409, {"ok": ok} if ok else {"error": "That task isn't waiting for an OK."})
        if path == "/api/paid-reset":
            name, kind = body.get("name"), body.get("kind") or "*"
            if not isinstance(name, str) or not isinstance(kind, str):
                return self._send(400, {"error": "name: a paid AI; kind: a task kind or * for every kind"})
            try:
                fleet.paid_reset(name, kind, by=login)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})
        if path.startswith("/api/only/") and path.rsplit("/", 1)[1].isdigit():
            names = body.get("providers") or []
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                return self._send(400, {"error": "providers: a list of paid AI names, or [] for any"})
            try:
                with fleet.conn() as c:
                    fleet.set_only(c, int(path.rsplit("/", 1)[1]), names, by=login)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})
        if path.startswith("/api/cancel/") and path.rsplit("/", 1)[1].isdigit():
            with fleet.conn() as c:
                ok = fleet.cancel(c, int(path.rsplit("/", 1)[1]), by=login)
            return self._send(200 if ok else 409, {"ok": ok} if ok else {"error": "That task is already over."})
        if path.startswith("/api/memory/delete/") and path.rsplit("/", 1)[1].isdigit():
            ok = fleet.forget(int(path.rsplit("/", 1)[1]))
            return self._send(200 if ok else 404, {"ok": ok} if ok else {"error": "No such note."})
        for verb in ("accept", "reject"):
            if path.startswith(f"/api/scores/{verb}/") and path.rsplit("/", 1)[1].isdigit():
                n = fleet.CATALOG.decide(fleet.STATE, {int(path.rsplit("/", 1)[1])}, verb == "accept", by=login)
                return self._send(200 if n else 409, {"ok": bool(n)} if n else {"error": "That score isn't waiting for you."})
        if path.startswith("/api/unlock/"):
            name = path.rsplit("/", 1)[1]
            if name not in fleet.policy()["frontier"]:
                return self._send(404, {"error": "No paid AI by that name."})
            ok = fleet.unlock(name)
            return self._send(200 if ok else 409, {"ok": ok} if ok else {"error": f"{name} isn't locked."})
        if path == "/api/budget/raise":
            try:
                return self._send(200, {"limit": fleet.raise_budget(str(body.get("provider") or ""), body.get("n") or 0,
                                                                    by=login)})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/stop":
            fleet.set_stop(bool(body.get("on")))
            return self._send(200, {"stop": (fleet.STATE / "STOP").exists()})
        if path == "/api/pause":
            fleet.set_pause(bool(body.get("on")))
            return self._send(200, {"paused": fleet.paused()})
        self._send(404, {"error": "not found"})


PAGE = (HERE / "web.html").read_text(encoding="utf-8") if (HERE / "web.html").exists() else "<p>web.html missing</p>"


def main():
    """Serves the page."""
    ap = argparse.ArgumentParser(description="fleet-tasks web page")
    ap.add_argument("--port", type=int, default=8960)
    a = ap.parse_args()
    if local_login():
        print(f"local login on: requests from this machine get in as {local_login()}", file=sys.stderr)
    elif not allowed_logins():
        print("FLEET_ALLOWED_LOGINS and FLEET_LOCAL_LOGIN are empty: nobody can open the page", file=sys.stderr)
    if os.environ.get("FLEET_BACKLOG_WORKER", "1") != "0":   # builds + autopilot (the switch starts off)
        threading.Thread(target=BACKLOG.loop, name="backlog", daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
