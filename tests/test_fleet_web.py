"""fleet-tasks web page (stacks/fleet-tasks/web.py): tailnet login rule, the
X-Fleet guard on changes, approve/deny/submit/stop, and what the state shows.
No Ollama: endpoints are stubbed."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "web.py"


@pytest.fixture()
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FLEET_ALLOWED_LOGINS", "owner@example.com")
    monkeypatch.setenv("FLEET_CLAUDE_LOGS", str(tmp_path / "no-logs"))
    spec = importlib.util.spec_from_file_location("fleet_web", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.USAGE, "copilot", lambda run=None: {"error": "not read in tests"})
    monkeypatch.setattr(mod.fleet, "endpoints", lambda: [
        {"name": "big", "url": "x", "model": "qwen3.6:27b", "tier": "large", "up": True, "loaded": ["qwen3.6:27b"]},
        {"name": "busy-box", "url": "x", "model": "llama3.1:8b", "tier": "small", "up": True, "loaded": ["other:24b"]},
        {"name": "sleeper", "url": "x", "model": "llama3.2:3b", "tier": "tiny", "up": False, "loaded": []}])
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield mod, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(base, path, body=None, login="owner@example.com", guard=True):
    headers = {"Content-Type": "application/json"}
    if login:
        headers["Tailscale-User-Login"] = login
    if guard:
        headers["X-Fleet"] = "1"
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}") if "json" in r.headers["Content-Type"] else r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_only_allowed_tailnet_logins(web):
    mod, base = web
    assert call(base, "/api/state", login=None)[0] == 403
    assert call(base, "/api/state", login="stranger@example.com")[0] == 403
    assert call(base, "/health", login=None)[0] == 200
    code, page = call(base, "/")
    assert code == 200 and "Fleet tasks" in page and "How to use" in page
    assert mod.decide("owner@example.com", "100.1.2.3", {"owner@example.com"})[0] is False   # not via tailscale serve


def test_changes_need_the_x_fleet_header(web):
    _, base = web
    assert call(base, "/api/submit", {"kind": "summarize", "prompt": "x"}, guard=False)[0] == 403
    code, out = call(base, "/api/submit", {"kind": "summarize", "prompt": "x"})
    assert code == 200 and out["id"] == 1
    assert call(base, "/api/submit", {"kind": "summarize", "prompt": "  "})[0] == 400


def test_state_explains_machines_and_budget(web):
    mod, base = web
    tid = mod.fleet.submit("draft", "write a scene")
    c = mod.fleet.db()
    mod.fleet.claim(c, "large", "big")
    s = call(base, "/api/state")[1]
    m = {x["name"]: x for x in s["machines"]}
    assert m["big"]["state"] == "working" and f"#{tid}" in m["big"]["why"]
    assert m["busy-box"]["state"] == "busy" and "never evicts" in m["busy-box"]["why"]
    assert m["sleeper"]["state"] == "down"
    assert {p["name"] for p in s["providers"]} == {"claude", "gemini", "copilot"}
    assert s["tasks"][0]["status_words"] == "being worked on"


def test_approve_and_deny_only_waiting_tasks(web):
    mod, base = web
    c = mod.fleet.db()
    a, b = mod.fleet.submit("hard", "q1"), mod.fleet.submit("hard", "q2")
    for tid in (a, b):
        c.execute("UPDATE tasks SET status='needs_approval' WHERE id=?", (tid,))
    assert [t["id"] for t in call(base, "/api/state")[1]["waiting"]] == [b, a]
    assert call(base, f"/api/approve/{a}", {})[0] == 200
    assert call(base, f"/api/approve/{a}", {})[0] == 409          # already decided
    assert call(base, f"/api/deny/{b}", {})[0] == 200
    rows = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM tasks")}
    assert rows == {a: "approved", b: "denied"}
    assert [e["event"] for e in mod.fleet.ledger_tail()] == ["deny", "approve"]


def test_stop_switch_blocks_the_frontier_gate(web):
    mod, base = web
    assert call(base, "/api/stop", {"on": True})[1]["stop"] is True
    tid = mod.fleet.submit("hard", "q")
    task = mod.fleet.db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert mod.fleet.gate(task, approved=True) == (None, "kill switch ~/.local/state/fleet-tasks/STOP is set")
    assert call(base, "/api/stop", {"on": False})[1]["stop"] is False


def test_submit_checks_kind_and_size(web):
    _, base = web
    assert call(base, "/api/submit", {"kind": "nonsense", "prompt": "x"})[0] == 400
    code, out = call(base, "/api/submit", {"kind": "summarize", "prompt": "x" * 100_001})
    assert code == 400 and "terminal" in out["error"]


def test_approve_records_the_tailnet_login_and_state_shows_the_total(web):
    mod, base = web
    tid = mod.fleet.submit("hard", "q")
    mod.fleet.db().execute("UPDATE tasks SET status='needs_approval' WHERE id=?", (tid,))
    assert call(base, f"/api/approve/{tid}", {})[0] == 200
    assert mod.fleet.ledger_tail()[0]["by"] == "owner@example.com"
    mod.fleet.ledger({"event": "call", "provider": "claude"})
    assert call(base, "/api/state")[1]["total"] == {"used": 1, "limit": mod.fleet.daily_total()}


def test_cancel_pause_and_why_it_moved_up(web):
    mod, base = web
    tid = mod.fleet.submit("summarize", "x", check=True)
    c = mod.fleet.db()
    mod.fleet.escalate(c, c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone(), "checker said no")
    t = call(base, "/api/state")[1]["tasks"][0]
    assert t["escalations"] == 1 and "checker said no" in t["escalated_why"]
    assert call(base, f"/api/cancel/{tid}", {}, guard=False)[0] == 403
    assert call(base, f"/api/cancel/{tid}", {})[0] == 200
    assert call(base, f"/api/cancel/{tid}", {})[0] == 409            # already over
    assert call(base, "/api/state")[1]["tasks"][0]["status_words"] == "you cancelled it"
    assert call(base, "/api/pause", {"on": True})[1]["paused"] is True
    assert call(base, "/api/state")[1]["paused"] is True
    assert call(base, "/api/pause", {"on": False})[1]["paused"] is False


def test_machine_card_shows_its_last_task(web):
    mod, base = web
    tid = mod.fleet.submit("draft", "x")
    c = mod.fleet.db()
    mod.fleet.claim(c, "large", "big")
    mod.fleet.finish(c, tid, "done", "ok", "qwen3.6:27b@big")
    m = {x["name"]: x for x in call(base, "/api/state")[1]["machines"]}
    assert m["big"]["last"] == {"id": tid, "status": "done", "ago_min": 0}
    assert m["sleeper"]["last"] is None


def test_every_control_has_a_tooltip_or_help():
    """Acceptance: every control has a tooltip or help entry. Fails when a
    button, input, select or textarea (in the HTML, or built in the script
    with el("button", ...)) has no title, no titled <label> around it, and
    no ? help panel."""
    import re
    from html.parser import HTMLParser
    html = (SRC.parent / "web.html").read_text(encoding="utf-8")

    class Walk(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bare = [], []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in ("button", "input", "select", "textarea"):
                labelled = any(t == "label" and "title" in x for t, x in self.stack)
                if not ("title" in a or "data-help" in a or labelled):
                    self.bare.append(f"<{tag} id={a.get('id')}>")
            if tag not in ("input", "br", "meta", "link"):
                self.stack.append((tag, a))

        def handle_endtag(self, tag):
            while self.stack and self.stack.pop()[0] != tag:
                pass

    w = Walk()
    w.feed(html)
    assert w.bare == []
    js_buttons = re.findall(r'el\(\s*"button"\s*,\s*\{([^}]*)\}', html)
    assert js_buttons and all("title:" in b for b in js_buttons)


def test_split_from_the_page_shows_progress_and_parts(web):
    mod, base = web
    code, out = call(base, "/api/submit", {"kind": "summarize", "prompt": "List errors",
                                           "split_text": "a" * 5000 + "\n\n" + "b" * 5000})
    assert code == 200
    pid = out["id"]
    c = mod.fleet.db()
    first = c.execute("SELECT id FROM tasks WHERE parent_id=? ORDER BY part", (pid,)).fetchone()["id"]
    mod.fleet.finish(c, first, "done", "E1")
    row = next(t for t in call(base, "/api/state")[1]["tasks"] if t["id"] == pid)
    assert row["status_words"] == "split into parts" and row["parts"]  == {"done": 1, "total": 2, "round": 0, "round_done": 1, "round_total": 2}
    detail = call(base, f"/api/task/{pid}")[1]
    assert [p["part"] for p in detail["parts"]] == [1, 2]
    assert call(base, "/api/submit", {"kind": "hard", "prompt": "x", "split_text": "a\n\nb"})[0] == 400


def test_submit_with_code_from_a_checkout(web, monkeypatch):
    mod, base = web
    monkeypatch.setattr(mod.fleet, "repos", lambda: {"demo": Path("/nowhere")})
    monkeypatch.setattr(mod.fleet, "repo_files", lambda name: ["src/api.py", "README.md"])
    got = {}

    def fake_source(kind, ask, repo, paths=(), pr=None, **kw):
        got.update(kind=kind, repo=repo, paths=paths, pr=pr)
        return mod.fleet.submit(kind, ask), 1
    monkeypatch.setattr(mod.fleet, "submit_source", fake_source)
    assert call(base, "/api/repos")[1] == {"repos": ["demo"]}
    assert call(base, "/api/repos/demo")[1] == {"paths": ["src/", "src/api.py", "README.md"], "total": 3}
    code, _ = call(base, "/api/submit", {"kind": "review", "prompt": "Find bugs", "repo": "demo",
                                        "paths": "src/api.py, README.md"})
    assert code == 200 and got == {"kind": "review", "repo": "demo", "paths": ["src/api.py", "README.md"], "pr": None}
    assert call(base, "/api/submit", {"kind": "review", "prompt": "x", "repo": "demo", "pr": "122"})[0] == 200
    assert got["pr"] == 122
    assert call(base, "/api/submit", {"kind": "review", "prompt": "x", "paths": "a.py"})[0] == 400
    assert call(base, "/api/submit", {"kind": "review", "prompt": "x", "repo": "demo", "pr": "abc"})[0] == 400
    assert call(base, "/api/submit", {"kind": "review", "prompt": "x", "repo": "demo", "split_text": "a\n\nb"})[0] == 400


def test_submit_accepts_paths_as_a_list(web, monkeypatch):
    mod, base = web
    got = {}
    monkeypatch.setattr(mod.fleet, "submit_source",
                        lambda kind, prompt, repo, paths, pr, **k: got.update(paths=paths) or (7, 1))
    code, _ = call(base, "/api/submit", {"kind": "review", "prompt": "x", "repo": "demo", "paths": ["a.py", "lib/b, c.py"]})
    assert code == 200 and got["paths"] == ["a.py", "lib/b, c.py"]      # a comma in a name stays


def test_a_split_job_is_one_queue_row_with_its_progress(web):
    mod, base = web
    pid = mod.fleet.submit_split("summarize", "Sum up", ["a", "b", "c"])
    s = call(base, "/api/state")[1]
    ids = [t["id"] for t in s["tasks"]]
    assert pid in ids and len(ids) == 1                      # the parts are inside the job, not in the queue
    row = s["tasks"][0]
    assert row["parts"]["total"] == 3 and row["parts"]["done"] == 0 and row["parts"]["round"] == 0


def test_voice_passes_a_recording_to_the_local_service_and_back(web):
    from http.server import BaseHTTPRequestHandler
    mod, base = web
    got = {}

    class Voice(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._json(200, {"ok": True})

        def do_POST(self):
            got["audio"] = self.rfile.read(int(self.headers["Content-Length"]))
            self._json(400, {"error": "couldn't read the recording"}) if got["audio"] == b"bad" else self._json(200, {"text": "check the logs"})

    import socket
    with socket.socket() as free:                                       # a port nothing listens on (the real service may be up)
        free.bind(("127.0.0.1", 0))
        mod.VOICE = f"http://127.0.0.1:{free.getsockname()[1]}"
    assert call(base, "/api/voice")[1] == {"ok": False}               # nothing on the voice port: the page hides its buttons
    assert call(base, "/api/listen", "x")[0] == 503
    vs = ThreadingHTTPServer(("127.0.0.1", 0), Voice)
    threading.Thread(target=vs.serve_forever, daemon=True).start()
    mod.VOICE = f"http://127.0.0.1:{vs.server_address[1]}"
    try:
        assert call(base, "/api/voice")[1] == {"ok": True}

        def post(data, guard=True):
            h = {"Tailscale-User-Login": "owner@example.com", "Content-Type": "application/octet-stream"}
            if guard:
                h["X-Fleet"] = "1"
            try:
                with urllib.request.urlopen(urllib.request.Request(base + "/api/listen", data, h), timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        assert post(b"\x1aE\xdf\xa3 opus") == (200, {"text": "check the logs"}) and got["audio"] == b"\x1aE\xdf\xa3 opus"
        assert post(b"bad")[0] == 400 and "Couldn't hear that" in post(b"bad")[1]["error"]
        assert post(b"x", guard=False)[0] == 403                          # same guard as every change
    finally:
        vs.shutdown()


def test_the_page_offers_voice_only_on_the_text_boxes():
    page = SRC.with_name("web.html").read_text()
    assert '["prompt", "splitText", "memQ", "fPrompt"].forEach(id => addMic($(id)))' in page and 'api("/api/voice")' in page


def test_voice_service_reads_real_audio():
    fw = pytest.importorskip("faster_whisper")
    spec = importlib.util.spec_from_file_location("fleet_voice", SRC.with_name("voice") / "voice.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(ValueError, match="couldn't read"):
        mod.transcribe(b"not audio")


def test_the_queue_pages_back_to_older_jobs_and_approvals_show_on_every_page(web):
    mod, base = web
    first = mod.fleet.submit("hard", "old job that needs an OK")       # 'hard' waits for the owner's OK
    with mod.fleet.conn() as c:
        c.execute("UPDATE tasks SET status='needs_approval' WHERE id=?", (first,))
    for i in range(mod.PAGE_ROWS + 5):
        mod.fleet.submit("summarize", f"job {i}")
    s = call(base, "/api/state")[1]
    assert s["queue_page"] == {"page": 0, "pages": 2, "jobs": mod.PAGE_ROWS + 6, "rows": mod.PAGE_ROWS}
    assert len(s["tasks"]) == mod.PAGE_ROWS and first not in [t["id"] for t in s["tasks"]]
    assert [t["id"] for t in s["waiting"]] == [first]                  # still asks for the OK, though it's on page 2
    older = call(base, "/api/state?page=1")[1]
    assert older["queue_page"]["page"] == 1 and older["tasks"][-1]["id"] == first and len(older["tasks"]) == 6
    assert call(base, "/api/state?page=99")[1]["queue_page"]["page"] == 1      # past the end: the last page
    assert call(base, "/api/state?page=-1")[1]["queue_page"]["page"] == 0


def test_job_watch_checks_have_their_own_section_and_stay_out_of_the_queue(web):
    mod, base = web
    mine = mod.fleet.submit("summarize", "my real job")
    ids = [mod.fleet.submit("watch", f"Job: PR reviews\nMeant to: every 10 minutes\nrun {i}") for i in range(mod.WATCH_ROWS + 3)]
    with mod.fleet.conn() as c:
        c.execute("UPDATE tasks SET status='done', result=? WHERE id=?", ('{\n"status": "ok",\n"note": ""\n}', ids[0]))
        c.execute("UPDATE tasks SET status='done', result=? WHERE id=?",
                  ('{"status": "problem", "note": "gh failed: auth"}', ids[-1]))
    s = call(base, "/api/state")[1]
    assert [t["id"] for t in s["tasks"]] == [mine] and s["queue_page"]["jobs"] == 1      # the queue is the owner's jobs only
    w = s["watch"]
    assert w["count"] == mod.WATCH_ROWS + 3 and w["pages"] == 2 and len(w["checks"]) == mod.WATCH_ROWS
    top = w["checks"][0]
    assert top["id"] == ids[-1] and top["job"] == "PR reviews" and top["verdict"] == "problem" and "auth" in top["note"]
    assert w["problems_today"] == 1                                    # the ok one isn't counted; queued ones have no verdict yet
    older = call(base, "/api/state?wpage=1")[1]["watch"]
    assert older["page"] == 1 and older["checks"][-1]["id"] == ids[0] and older["checks"][-1]["verdict"] == "ok"


def test_follow_ups_and_documents_from_the_page(web):
    import base64
    mod, base = web
    first = mod.fleet.submit("summarize", "Review the store")
    assert call(base, f"/api/followup/{first}", {"prompt": "more"})[0] == 400              # no answer yet
    with mod.fleet.conn() as c:
        c.execute("UPDATE tasks SET status='done', result=? WHERE id=?",
                  ("## Next steps\n1. Replace the banner text\n2. Add safety lines", first))
    d = call(base, f"/api/task/{first}")[1]
    assert d["suggestions"] == ["Replace the banner text", "Add safety lines"] and d["followups"] == []
    code, r = call(base, f"/api/followup/{first}", {"prompt": "Work on: Add safety lines", "kind": "draft"})
    assert code == 200
    d = call(base, f"/api/task/{first}")[1]
    assert [x["id"] for x in d["followups"]] == [r["id"]] and d["followups"][0]["ask"] == "Work on: Add safety lines"
    assert call(base, f"/api/task/{r['id']}")[1]["uses"] == str(first)
    assert call(base, "/api/state")[1]["tasks"][0]["uses"] == str(first)                    # the queue row says so
    assert [e["id"] for e in call(base, "/api/state")[1]["earlier"]] == [first]           # the picker lists finished jobs
    assert call(base, f"/api/followup/{first}", {"prompt": "x", "kind": "nope"})[0] == 400
    assert call(base, f"/api/followup/{first}", {"prompt": "x"}, guard=False)[0] == 403

    doc = {"name": "../notes.md", "data": base64.b64encode(b"# Notes\n\nOrder more PLA.").decode(),
           "kind": "summarize", "prompt": "Make a shopping list"}
    code, r = call(base, "/api/document", doc)
    assert code == 200 and "read notes.md" in r["notes"][0]                              # the path is dropped
    t = call(base, f"/api/task/{r['id']}")[1]
    assert t["source"] == "document: notes.md" and "Order more PLA." in t["prompt"] and t["ask"] == "Make a shopping list"
    assert call(base, "/api/document", dict(doc, name="x.exe"))[0] == 400
    assert call(base, "/api/document", dict(doc, data="not base64!"))[0] == 400
    assert call(base, "/api/document", dict(doc, prompt=""))[0] == 400


def test_a_question_can_use_earlier_work_by_picking_or_writing_numbers(web):
    mod, base = web
    a = mod.fleet.submit("summarize", "Review the store")
    with mod.fleet.conn() as c:
        c.execute("UPDATE tasks SET status='done', result='Store answer' WHERE id=?", (a,))
    code, r = call(base, "/api/submit", {"kind": "summarize", "prompt": f"What should I fix first from #{a}? Also #424242"})
    assert code == 200 and r["notes"][0] == f"builds on #{a}" and "#424242 isn't a finished task" in r["notes"][1]
    assert call(base, f"/api/task/{r['id']}")[1]["uses"] == str(a)
    code, r = call(base, "/api/submit", {"kind": "summarize", "prompt": "Turn it into a checklist", "use": f"{a}"})
    assert code == 200 and "Store answer" in call(base, f"/api/task/{r['id']}")[1]["prompt"]
    assert call(base, "/api/submit", {"kind": "summarize", "prompt": "x", "use": "999999"})[0] == 400
    assert call(base, "/api/submit", {"kind": "research", "prompt": "x", "use": str(a), "urls": "https://example.com"})[0] == 400
    code, r = call(base, "/api/submit", {"kind": "summarize", "prompt": "No refs here"})
    assert code == 200 and "notes" not in r
    html = mod.DOC.to_html(f"See #{a} and `#{a}` and [x](https://e.com/#{a})")
    assert html.count(f'href="/#task={a}"') == 1                                         # not in code or a link


def test_raise_one_ai_tools_limit_from_the_page(web):
    mod, base = web
    assert call(base, "/api/budget/raise", {"provider": "gemini", "n": 5}, guard=False)[0] == 403
    code, r = call(base, "/api/budget/raise", {"provider": "gemini", "n": 5})
    assert code == 200 and r["limit"] == mod.fleet.daily_limit("gemini")
    gem = next(p for p in call(base, "/api/state")[1]["providers"] if p["name"] == "gemini")
    assert gem["raised"] == 5
    assert call(base, "/api/budget/raise", {"provider": "gemini", "n": 0})[0] == 400
    assert call(base, "/api/budget/raise", {"provider": "gemini", "n": 99})[0] == 200    # no upper limit (owner 2026-10-28)
    assert call(base, "/api/budget/raise", {"provider": "nope", "n": 1})[0] == 400
    assert [e["by"] for e in mod.fleet.read_ledger() if e.get("event") == "raise"] == ["owner@example.com"] * 2


def test_limit_a_task_to_one_paid_ai_from_the_page(web):
    mod, base = web
    tid = mod.fleet.submit("hard", "x")
    assert call(base, f"/api/only/{tid}", {"providers": ["gemini"]}, guard=False)[0] == 403
    assert call(base, f"/api/only/{tid}", {"providers": ["gemini"]})[0] == 200
    assert call(base, f"/api/task/{tid}")[1]["only"] == "gemini"
    assert call(base, f"/api/only/{tid}", {"providers": ["nope"]})[0] == 400
    assert call(base, f"/api/only/{tid}", {"providers": "gemini"})[0] == 400
    assert call(base, f"/api/only/{tid}", {"providers": []})[0] == 200
    assert call(base, f"/api/task/{tid}")[1]["only"] == ""


def test_designs_from_the_page(web):
    mod, base = web
    idea = mod.fleet.submit("hard", "x")
    with mod.fleet.conn() as c:
        c.execute("UPDATE tasks SET status='done', result='a dice tray' WHERE id=?", (idea,))
    assert call(base, "/api/design/start", {"task": idea}, guard=False)[0] == 403
    assert call(base, "/api/design/start", {"task": 99999})[0] == 400
    code, r = call(base, "/api/design/start", {"task": idea, "focus": "dice tray"})
    assert code == 200
    did = r["id"]
    assert call(base, "/api/designs")[1]["designs"][0]["id"] == did
    assert call(base, f"/api/design/{did}/approve", {})[0] == 400              # not ready to look at
    assert call(base, f"/api/design/{did}/render.png")[0] == 404
    out = mod.fleet.STATE / "designs" / str(did)
    out.mkdir(parents=True)
    (out / "render.png").write_bytes(b"\x89PNG fake")
    req = urllib.request.Request(base + f"/api/design/{did}/render.png", headers={"Tailscale-User-Login": "owner@example.com"})
    with urllib.request.urlopen(req) as resp:
        assert resp.headers["Content-Type"] == "image/png" and resp.read() == b"\x89PNG fake"
    assert call(base, f"/api/design/{did}/cancel", {})[0] == 200
    assert call(base, f"/api/design/{did}/cancel", {})[0] == 409


def test_backlog_endpoints_check_input_and_pass_board_refusals(web, monkeypatch):
    mod, base = web
    seen = {}
    monkeypatch.setattr(mod.BACKLOG, "state", lambda: {"items": [], "products": []})
    monkeypatch.setattr(mod.BACKLOG, "assign", lambda wp, to: seen.setdefault("assign", (wp, to)) and "route")
    def refuse(wp, builder=None, by=None):
        raise mod.BACKLOG_MOD.BOARD.BoardError("WP-9 is claimed by someone")
    monkeypatch.setattr(mod.BACKLOG, "request_build", refuse)
    assert call(base, "/api/backlog") == (200, {"items": [], "products": []})
    assert call(base, "/api/backlog/assign/WP-27", {"to": "local"})[0] == 200 and seen["assign"] == ("WP-27", "local")
    assert call(base, "/api/backlog/assign/../x", {"to": "local"})[0] == 400
    assert call(base, "/api/backlog/build/WP-9", {}) == (409, {"error": "WP-9 is claimed by someone"})
    assert call(base, "/api/backlog/plan/WP-27", [1, 2])[0] == 400          # not an object
    assert call(base, "/api/backlog/autopilot", {"on": True}, guard=False)[0] == 403
    assert call(base, "/api/backlog/autopilot", {"on": True}) == (200, {"ok": True, "on": True})
    assert (mod.fleet.STATE / "AUTOPILOT").exists()
    assert call(base, "/api/backlog/frobnicate/WP-1", {})[0] == 404


def test_local_login_is_opt_in_and_only_from_this_machine(web, monkeypatch):
    mod, base = web
    assert call(base, "/api/state", login=None)[0] == 403                    # off by default
    monkeypatch.setenv("FLEET_LOCAL_LOGIN", "Me")
    assert call(base, "/api/state", login=None)[0] == 200                    # this machine, no header
    assert call(base, "/api/state", login="stranger@example.com")[0] == 403  # a header still has to be allowed
    assert mod.decide(None, "203.0.113.5", set(), "me")[0] is False         # another machine never gets in
    assert mod.decide(None, "127.0.0.1", set(), "me") == (True, "me")
