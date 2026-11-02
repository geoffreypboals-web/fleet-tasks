"""fleet-tasks terminal view (stacks/fleet-tasks/top.py, board WP-152 step 3b):
the screen in each state, the typed commands with their y/N questions, the
new fleet.py stop/budget/top commands, and that every control on the page
has a terminal equivalent. No Ollama: endpoints are stubbed."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

LIBENV = {k: v for k, v in os.environ.items() if k == "LD_LIBRARY_PATH"}  # the runner's Python needs its libpython

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture()
def top(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("USER", "owner")
    monkeypatch.setenv("FLEET_CLAUDE_LOGS", str(tmp_path / "no-logs"))
    spec = importlib.util.spec_from_file_location("fleet_top", HERE / "top.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.web.USAGE, "copilot", lambda run=None: {"error": "not read in tests"})
    monkeypatch.setattr(mod.fleet, "endpoints", lambda fresh=False: [
        {"name": "big", "url": "x", "model": "qwen3.6:27b", "tier": "large", "up": True, "loaded": ["qwen3.6:27b"]},
        {"name": "sleeper", "url": "x", "model": "llama3.2:3b", "tier": "tiny", "up": False, "loaded": []}])
    return mod


def screen(top):
    return top.render(top.web.state(), colour=False, width=160)


def waiting(top, prompt="think hard"):
    f = top.fleet
    tid = f.submit("hard", prompt)
    with f.conn() as c:
        f.finish(c, tid, "needs_approval")
        f.note(c, tid, "hard may use claude only with the owner's OK")
    return tid


def test_idle_screen(top):
    s = screen(top)
    assert "running" in s and "MACHINES" in s and "big" in s and "free" in s and "down" in s
    assert "PAID AI TODAY  0 calls" in s and "no tasks yet" in s and "a ID approve" in s
    assert "\033[" not in s                       # no colour codes in plain output


def test_busy_waiting_paused_stopped(top):
    f = top.fleet
    f.submit("summarize", "x")
    tid = waiting(top, "please think hard about it")
    f.set_pause(True)
    f.set_stop(True)
    s = screen(top)
    assert "PAUSED" in s and "PAID AI STOPPED" in s
    assert "WAITING FOR YOUR OK" in s and f"#{tid}" in s and "please think hard" in s and "owner's OK" in s
    assert "waiting for a free machine" in s


def test_approve_asks_first_and_records_terminal_user(top):
    tid = waiting(top)
    msg, quit_ = top.act(f"a {tid}", read=lambda q: "n")
    assert msg == "not approved" and not quit_
    msg, _ = top.act(f"a {tid}", read=lambda q: "y")
    assert msg == f"#{tid} approved"
    with top.fleet.conn() as c:
        assert c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "approved"
    entries = top.fleet.read_ledger()
    assert any(e.get("event") == "approve" and "terminal:owner" in str(e.get("by", "")) for e in entries)


def test_deny_cancel_and_bad_input(top):
    tid = waiting(top)
    assert top.act(f"d {tid}")[0] == f"#{tid} denied"
    assert "isn't waiting" in top.act(f"d {tid}")[0]
    other = top.fleet.submit("draft", "x")
    assert top.act(f"c #{other}")[0] == f"#{other} cancelled"
    assert "already over" in top.act(f"c {other}")[0]
    assert "needs a task number" in top.act("a")[0]
    assert "unknown command" in top.act("zap")[0]
    assert top.act("q") == ("", True) and top.act("") == ("", False)
    assert "commands:" in top.act("?")[0]


def test_pause_and_stop_toggle_only_after_yes(top):
    f = top.fleet
    assert top.act("p", read=lambda q: "")[0] == "unchanged" and not f.paused()
    assert top.act("p", read=lambda q: "y")[0] == "paused" and f.paused()
    assert top.act("p", read=lambda q: "yes")[0] == "resumed" and not f.paused()
    assert top.act("s", read=lambda q: "y")[0] == "paid AI stopped" and (f.STATE / "STOP").exists()
    assert top.act("s", read=lambda q: "y")[0] == "paid AI allowed" and not (f.STATE / "STOP").exists()


def cli(tmp_path, *args):
    return subprocess.run([sys.executable, str(HERE / "fleet.py"), *args], capture_output=True, text=True, timeout=60,
                          env={"FLEET_STATE": str(tmp_path / "s"), "GPU_LEASE_DIR": str(tmp_path / "l"),
                               "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **LIBENV}, check=False)


def test_cli_stop_budget_and_plain_top(tmp_path):
    assert "stopped" in cli(tmp_path, "stop", "on").stdout and (tmp_path / "s" / "STOP").exists()
    assert "STOPPED" in cli(tmp_path, "budget").stdout
    assert "allowed" in cli(tmp_path, "stop", "off").stdout and not (tmp_path / "s" / "STOP").exists()
    r = cli(tmp_path, "top", "--plain")
    assert r.returncode == 0 and "MACHINES" in r.stdout and "PAID AI TODAY" in r.stdout


def test_every_page_control_has_a_terminal_command():
    """The page and the terminal can't drift apart: each POST the page can
    send, and each GET it reads, maps to a fleet.py command."""
    web = (HERE / "web.py").read_text()
    fleet = (HERE / "fleet.py").read_text()
    commands = set(re.findall(r'sub\.add_parser\("([\w-]+)"', fleet)) | \
        {m for m in re.findall(r'for name in \(([^)]*)\)', fleet) for m in re.findall(r'"(\w+)"', m)}
    page_to_cli = {"submit": "submit", "approve": "approve", "deny": "deny", "cancel": "cancel", "stop": "stop",
                   "pause": "pause", "memory/delete": "forget", "state": "top", "task": "status", "repos": "repos",
                   "memory": "recall", "unlock": "unlock", "models": "scores", "scores": "scores",
                   "followup": "followup", "document": "submit", "budget": "budget", "only": "only", "design": "design", "designs": "design",
                   "backlog": "backlog"}
    routes = set(re.findall(r'"/api/([a-z]+(?:/delete)?)[/"]', web)) | \
        {v for v in re.findall(r'f"/api/\{verb\}/', web) for v in ("approve", "deny")}
    assert routes, "no /api routes found in web.py"
    typing_aids = {"voice", "listen"}         # speech to text for a phone's text boxes; nothing to do in a terminal
    routes -= typing_aids
    unmapped = [r for r in routes if r not in page_to_cli]
    assert not unmapped, f"new page routes need a terminal command and an entry here: {unmapped}"
    missing = [page_to_cli[r] for r in routes if page_to_cli[r] not in commands]
    assert not missing, f"fleet.py is missing: {missing}"


def test_install_copies_every_file_the_program_loads():
    """2026-10-28: usage.py was missing from install.sh, so the deployed page failed to start."""
    install = (HERE / "install.sh").read_text()
    loaded = set()
    for f in ("fleet.py", "web.py", "top.py", "mcp.py", "doctor.py", "backlog.py"):
        loaded |= set(re.findall(r'HERE / "([\w.]+\.(?:py|html|json))"', (HERE / f).read_text()))
    loaded -= {"policy.json"}                                   # installed on its own line, never overwritten
    missing = [n for n in sorted(loaded) if n not in install]
    assert not missing, f"install.sh doesn't copy {missing}"
