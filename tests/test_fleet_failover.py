"""Paid-AI lockout failover (stacks/fleet-tasks/fleet.py, board WP-152 step 3d):
fake claude, gemini and copilot CLIs that answer, run out of usage, time out
or fail, and the order, caps, locks and ledger that follow."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
SRC = HERE / "fleet.py"

# What the real CLIs print when their plan's usage is used up (kept as fixtures; see RUNBOOK).
CLAUDE_LIMIT = {"type": "result", "subtype": "success", "is_error": True,
                "result": "Claude AI usage limit reached|{epoch}"}
CLAUDE_LIMIT_NEW = "You've hit your session limit · resets 3pm (America/New_York)"
GEMINI_LIMIT = ('Error: [{"error": {"code": 429, "message": "Quota exceeded for quota metric \'Gemini 2.5 Pro '
                'Requests\' and limit \'Requests per day\'", "status": "RESOURCE_EXHAUSTED"}}]')
COPILOT_LIMIT = "Error: You have exceeded your monthly quota of premium requests. Upgrade or wait for the reset."

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
name, d = Path(sys.argv[0]).name, Path(os.environ["FAKE_DIR"])
if "--version" in sys.argv:
    v = d / f"version-{name}"
    print(v.read_text() if v.exists() else "GitHub Copilot CLI 1.0.103."); sys.exit(0)
modes = (d / f"mode-{name}").read_text().split() if (d / f"mode-{name}").exists() else ["ok"]
mode = modes[0]
(d / f"mode-{name}").write_text(" ".join(modes[1:] or modes))       # one mode per call; the last one repeats
stdin = sys.stdin.read()
with open(d / f"log-{name}.jsonl", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}) + "\n")
text = (d / f"text-{name}").read_text() if (d / f"text-{name}").exists() else ""
if mode == "ok":
    ans = f"answer from {name}"
    print(json.dumps({"result": ans, "response": ans}) if name != "copilot" else ans)
elif mode == "lock-json":                   # Claude Code: exit 0, is_error, the limit as its result
    print(text)
elif mode == "lock":
    print(text, file=sys.stderr); sys.exit(1)
elif mode == "flaky":
    print("Error: fetch failed (ECONNRESET)", file=sys.stderr); sys.exit(1)
elif mode == "slow":
    time.sleep(5)
elif mode == "broken":
    print("Error: unknown option --frobnicate", file=sys.stderr); sys.exit(2)
'''


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("claude", "gemini", "copilot"):
        (bin_ / name).write_text(FAKE)
        (bin_ / name).chmod(0o755)
    pol = json.loads((HERE / "tests" / "policy.json").read_text())
    for name, prov in pol["frontier"].items():
        prov["cmd"] = [str(bin_ / name)] + prov["cmd"][1:]
        if "version_cmd" in prov:
            prov["version_cmd"] = [str(bin_ / name)] + prov["version_cmd"][1:]
        prov["timeout"] = 2
    pol["frontier"]["gemini"]["enabled"] = True
    (tmp_path / "policy.json").write_text(json.dumps(pol))
    monkeypatch.setenv("FLEET_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("FLEET_PHONE", "off")
    spec = importlib.util.spec_from_file_location("fleet", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def mode(name, *modes, text=None):
        (tmp_path / f"mode-{name}").write_text(" ".join(modes))
        if text is not None:
            (tmp_path / f"text-{name}").write_text(text)

    def calls(name):
        f = tmp_path / f"log-{name}.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def policy(edit):
        p = json.loads(mod.POLICY.read_text())
        edit(p)
        mod.POLICY.write_text(json.dumps(p))

    return mod, mode, calls, policy


def task(fleet, tid):
    return fleet.db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def run(fleet, tid, approved=True):
    fleet.work_frontier(fleet.db(), task(fleet, tid), approved=approved)
    return task(fleet, tid)


def test_claude_then_gemini_lock_out_and_copilot_answers_with_the_owners_ok(env):
    fleet, mode, calls, _ = env
    epoch = int(time.time()) + 3 * 3600
    mode("claude", "lock-json", text=json.dumps(CLAUDE_LIMIT).replace("{epoch}", str(epoch)))
    mode("gemini", "lock", text=GEMINI_LIMIT)
    tid = fleet.submit("hard", "what is 2+2")
    assert run(fleet, tid, approved=False)["status"] == "needs_approval"
    assert fleet.decide(fleet.db(), tid, True, by="owner@example.com")
    t = run(fleet, tid)
    assert t["status"] == "done" and t["by"] == "copilot" and t["result"].strip() == "answer from copilot"
    assert "claude is locked out" in t["notes"] and "gemini is locked out" in t["notes"]
    lk = fleet.locks()
    assert lk["claude"]["until"] == datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
    assert lk["gemini"]["until"] > fleet.now()                      # midnight Pacific, whatever today is
    # locked calls are given back: only Copilot's call counts, today and against the total
    assert fleet.calls_today() == 1 and fleet.calls_today("claude") == 0 and fleet.calls_month("copilot") == 1
    call = [e for e in fleet.read_ledger() if e.get("event") == "call" and e["provider"] == "copilot"][0]
    assert call["approved_by"] == "owner@example.com" and len(call["after"]) == 2
    assert call["after"][0].startswith("claude locked until")


def test_copilot_is_never_picked_while_claude_or_gemini_can_answer(env):
    fleet, mode, calls, _ = env
    assert run(fleet, fleet.submit("hard", "x"))["by"] == "claude"
    mode("claude", "lock", text=CLAUDE_LIMIT_NEW)
    assert run(fleet, fleet.submit("hard", "x"))["by"] == "gemini"
    assert run(fleet, fleet.submit("hard", "x"))["by"] == "gemini"   # claude stays locked: not even tried
    assert len(calls("claude")) == 2 and calls("copilot") == []


def test_kind_order_is_the_policy_order_and_watch_never_reaches_paid_ai(env):
    fleet, *_ = env
    assert fleet.providers_for("hard") == ["claude", "gemini", "copilot"]
    assert fleet.providers_for("code") == ["claude", "copilot"]       # code never listed gemini
    assert fleet.providers_for("watch") == []


def test_copilot_gets_the_prompt_as_an_argument_with_no_tools_in_an_empty_folder(env):
    fleet, mode, calls, policy = env
    policy(lambda p: [p["frontier"][n].update(enabled=False) for n in ("claude", "gemini")])
    run(fleet, fleet.submit("hard", "the question", system="be brief"))
    c = calls("copilot")[0]
    assert c["argv"][c["argv"].index("-p") + 1] == "be brief\n\nthe question" and c["stdin"] == ""
    assert "--available-tools=fleet_text_only" in c["argv"] and "--disable-builtin-mcps" in c["argv"]
    assert c["cwd"] == str(fleet.STATE / "empty-cwd") and not os.listdir(c["cwd"])
    assert calls("copilot") and fleet.calls_month("copilot") == 1


def test_monthly_cap_holds_copilot_and_the_task_waits_with_its_ok(env):
    fleet, mode, calls, _ = env
    mode("claude", "lock", text=CLAUDE_LIMIT_NEW)
    mode("gemini", "lock", text=GEMINI_LIMIT)
    for _ in range(10):
        fleet.ledger({"event": "call", "provider": "copilot"})
    # the 10 copilot calls also fill today's total, so start the month earlier
    entries = fleet.read_ledger()
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=1)
    if month_start.date() == datetime.now().date():
        pytest.skip("on the 1st the month's calls are all today's")
    (fleet.STATE / "frontier-ledger.jsonl").write_text(
        "".join(json.dumps(dict(e, at=month_start.isoformat(timespec="seconds"))) + "\n" for e in entries))
    tid = fleet.submit("hard", "x")
    t = run(fleet, tid)
    assert t["status"] == "approved" and calls("copilot") == []
    assert "copilot used this month's 10 calls" in t["notes"] and "claude locked until" in t["notes"]
    assert t["wait_until"] > time.time()
    c = fleet.db()                                                   # the loop leaves it alone until then
    where = "status IN ('queued','approved') AND (tier='frontier' OR status='approved') AND wait_until <= ? ORDER BY id"
    assert fleet.take(c, where, (time.time(),), "frontier") is None


def test_all_locked_then_unlock_lets_the_waiting_task_run(env):
    fleet, mode, calls, policy = env
    policy(lambda p: p["frontier"]["copilot"].update(enabled=False))
    sent = []
    fleet.PHONE = "phone"
    fleet.send_to_phone = lambda title, text: sent.append(title) or True
    mode("claude", "lock", "ok", text=CLAUDE_LIMIT_NEW)
    mode("gemini", "lock", text=GEMINI_LIMIT)
    tid = fleet.submit("hard", "x")
    t = run(fleet, tid)
    assert t["status"] == "approved" and "paid AI unavailable" in t["notes"] and t["wait_until"] > time.time()
    time.sleep(0.2)                                                  # the alert goes on its own thread
    assert sent == ["Fleet: every paid AI is locked out"]
    assert not fleet.budget_left()
    assert fleet.unlock("claude") and not fleet.unlock("claude")
    assert task(fleet, tid)["wait_until"] == 0 and fleet.budget_left()
    assert run(fleet, tid)["by"] == "claude"


def test_network_failure_gets_one_retry_then_the_next_provider(env):
    fleet, mode, calls, _ = env
    mode("claude", "flaky", "ok")
    assert run(fleet, fleet.submit("hard", "x"))["by"] == "claude" and len(calls("claude")) == 2
    mode("claude", "slow")                                           # times out (2 s) twice
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["by"] == "gemini" and "claude failed again" in t["notes"]
    assert "claude" not in fleet.locks()                             # a timeout is never a lock


def test_other_errors_fail_the_task_and_are_logged(env):
    fleet, mode, calls, _ = env
    mode("claude", "broken")
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["status"] == "failed" and "frobnicate" in t["notes"] and calls("gemini") == []
    assert any(e.get("event") == "error" and "frobnicate" in e.get("error", "") for e in fleet.read_ledger())


def test_an_answer_that_mentions_quota_is_just_an_answer(env):
    fleet, mode, calls, _ = env
    assert fleet.classify("claude", "429 Too Many Requests") == "lockout"
    assert fleet.classify("gemini", GEMINI_LIMIT) == "lockout"
    assert fleet.classify("copilot", COPILOT_LIMIT) == "lockout"
    assert fleet.classify("claude", "API Error: 529 overloaded_error") == "transient"
    assert fleet.classify("claude", "Error: unknown option") is None
    tid = fleet.submit("hard", "explain API quota and rate limit errors")
    assert run(fleet, tid)["status"] == "done" and not fleet.locks()


def test_reset_times_from_the_message_and_the_defaults(env):
    fleet, *_ = env
    soon = datetime.now() + timedelta(seconds=30)
    assert abs((fleet.reset_from("Please retry in 30s") - soon).total_seconds()) < 3
    assert abs((fleet.reset_from("try again in 2 hours") - datetime.now() - timedelta(hours=2)).total_seconds()) < 3
    assert abs((fleet.reset_from('{"retry-after": "120"}') - datetime.now() - timedelta(seconds=120)).total_seconds()) < 3
    assert fleet.reset_from("Claude AI usage limit reached|1900000000") == datetime.fromtimestamp(1900000000)
    at = fleet.reset_from(CLAUDE_LIMIT_NEW)
    # up to a day ahead, plus an hour when a DST change falls in between
    assert at > datetime.now() and at - datetime.now() <= timedelta(days=1, hours=1)
    assert fleet.reset_from("quota exceeded") is None
    nm = fleet.default_reset("copilot")
    assert nm.day == 1 and nm > datetime.now() and nm.hour == 0
    assert timedelta(hours=4.9) < fleet.default_reset("claude") - datetime.now() < timedelta(hours=5.1)
    assert timedelta(0) < fleet.default_reset("gemini") - datetime.now() <= timedelta(days=1, hours=1)
    assert timedelta(minutes=59) < fleet.default_reset("someone-new") - datetime.now() <= timedelta(hours=1)


def test_policy_can_add_lockout_patterns_and_reset_rules(env):
    fleet, mode, calls, policy = env
    policy(lambda p: p["frontier"]["claude"].update(lockout=["plan paused"], reset="hours:2"))
    mode("claude", "lock", text="Error: plan paused by admin")
    run(fleet, fleet.submit("hard", "x"))
    until = datetime.fromisoformat(fleet.locks()["claude"]["until"])
    assert timedelta(hours=1.9) < until - datetime.now() < timedelta(hours=2.1)


def test_unlock_from_the_terminal(env):
    fleet, mode, *_ = env
    fleet.lock("gemini", datetime.now() + timedelta(hours=1), "quota")
    env_vars = dict(os.environ)
    out = subprocess.run([sys.executable, str(SRC), "budget"], capture_output=True, text=True, env=env_vars)
    assert "gemini" in out.stdout and "LOCKED until" in out.stdout and "this month" in out.stdout
    out = subprocess.run([sys.executable, str(SRC), "unlock", "gemini"], capture_output=True, text=True, env=env_vars)
    assert "gemini unlocked" in out.stdout and "gemini" not in fleet.locks()
    out = subprocess.run([sys.executable, str(SRC), "unlock", "nobody"], capture_output=True, text=True, env=env_vars)
    assert out.returncode == 2


def test_state_for_the_page_shows_caps_and_locks(env):
    fleet, *_ = env
    fleet.lock("claude", datetime.now() + timedelta(hours=1), "usage limit reached")
    rows = {r["name"]: r for r in fleet.providers_state()}
    assert rows["claude"]["locked_until"] and rows["claude"]["lock_reason"] == "usage limit reached"
    assert rows["copilot"]["last_resort"] and rows["copilot"]["monthly"] == 10 and rows["copilot"]["month"] == 0
    assert rows["gemini"]["monthly"] is None
    assert "LOCKED until" in fleet.paid_line(rows["claude"]) and "last resort" in fleet.paid_line(rows["copilot"])


def test_copilot_tool_filter_is_never_empty():
    """Live check 2026-10-26: an empty --available-tools= leaves every tool on
    (Copilot ran `id`); naming a tool that doesn't exist leaves none."""
    cmd = json.loads((HERE / "tests" / "policy.json").read_text())["frontier"]["copilot"]["cmd"]
    flags = [x for x in cmd if x.startswith("--available-tools")]
    assert flags and all("=" in f and f.split("=", 1)[1] for f in flags)
    assert "--allow-all" not in cmd and "--allow-all-tools" not in cmd


def test_an_empty_tool_filter_is_refused_before_any_call(env):
    fleet, mode, calls, policy = env
    policy(lambda p: [p["frontier"][n].update(enabled=False) for n in ("claude", "gemini")])
    policy(lambda p: p["frontier"]["copilot"].update(cmd=[x if not x.startswith("--available-tools") else
                                                          "--available-tools=" for x in p["frontier"]["copilot"]["cmd"]]))
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["status"] == "failed" and "leaves every tool on" in t["notes"] and calls("copilot") == []
    assert fleet.calls_today() == 0


def test_an_unchecked_copilot_version_is_locked_out_without_a_call(env, tmp_path):
    fleet, mode, calls, policy = env
    policy(lambda p: [p["frontier"][n].update(enabled=False) for n in ("claude", "gemini")])
    (tmp_path / "version-copilot").write_text("GitHub Copilot CLI 1.0.110.")
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["status"] == "approved" and calls("copilot") == []
    lk = fleet.locks()["copilot"]
    assert "1.0.110" in lk["reason"] and "not the checked 1.0.103" in lk["reason"]
    assert fleet.calls_today() == 0 and fleet.calls_month("copilot") == 0      # no call made, none given back
    assert "--no-auto-update" in json.loads(fleet.POLICY.read_text())["frontier"]["copilot"]["cmd"]


GEMINI_BUSY = ("Attempt 1 failed. RetryableQuotaError: This model is currently experiencing high demand. "
               "Spikes in demand are usually temporary. Please try again later.")


def test_a_busy_gemini_is_a_short_wait_and_copilot_is_kept_for_real_lockouts(env):
    """2026-10-27 #319: Gemini's "high demand" matched 'quota', locked it until midnight, and the
    tasks went to Copilot, the 10-a-month last resort."""
    fleet, mode, calls, _ = env
    assert fleet.classify("gemini", GEMINI_BUSY) == "busy"
    mode("claude", "lock", text=CLAUDE_LIMIT_NEW)
    mode("gemini", "lock", text=GEMINI_BUSY)
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["status"] == "approved" and calls("copilot") == []                    # waits instead
    assert "gemini is busy until" in t["notes"]
    g = fleet.locks()["gemini"]
    assert g.get("busy") and g["until"] <= (datetime.now() + timedelta(minutes=31)).isoformat(timespec="seconds")
    assert fleet.calls_today("gemini") == 0                                        # a busy call is given back
    fleet.unlock("gemini")
    mode("gemini", "ok")
    assert run(fleet, t["id"])["by"] == "gemini"


def test_an_answer_that_is_only_tool_calls_fails_the_task(env):
    fleet, mode, calls, _ = env
    mode("claude", "lock", text=CLAUDE_LIMIT_NEW)
    mode("gemini", "lock", text=GEMINI_LIMIT)
    mode("copilot", "lock-json", text='<invoke name="bash">\n<parameter name="command">which openscad</parameter>\n</invoke>')
    t = run(fleet, fleet.submit("hard", "x"))
    assert t["status"] == "failed" and "tool calls written out as text" in t["notes"]


def test_a_task_limited_to_gemini_never_goes_to_claude_or_copilot(env):
    """2026-10-27: the owner asked for Gemini to write the design prompts; while Gemini
    was busy they went to Claude. 'Gemini only' waits for Gemini instead."""
    fleet, mode, calls, _ = env
    tid = fleet.submit("hard", "x")
    with fleet.conn() as c:
        fleet.set_only(c, tid, ["gemini"], by="owner@example.com")
        with pytest.raises(ValueError, match="no paid AI called"):
            fleet.set_only(c, tid, ["nope"])
    mode("gemini", "lock", text=GEMINI_BUSY)
    t = run(fleet, tid)
    assert t["status"] == "approved" and calls("claude") == [] and calls("copilot") == []
    assert "only gemini" in t["notes"] and t["wait_until"] > time.time()
    fleet.unlock("gemini")
    mode("gemini", "ok")
    assert run(fleet, tid)["by"] == "gemini"
    assert [e["providers"] for e in fleet.read_ledger() if e.get("event") == "only"] == [["gemini"]]
    other = fleet.submit("hard", "y")                                   # an unlimited task: the usual order
    assert run(fleet, other)["by"] == "claude"
    with fleet.conn() as c:
        fleet.set_only(c, other, [])
    assert fleet.only_providers(task(fleet, other)) == []


def test_a_task_whose_only_ai_is_mid_call_goes_back_for_a_moment(env):
    """One call per paid AI at a time: a Gemini-only task while Gemini answers
    another task waits a few seconds, not the budget's 30 minutes, and never
    goes to Claude or Copilot."""
    fleet, mode, calls, _ = env
    tid = fleet.submit("hard", "x")
    with fleet.conn() as c:
        fleet.set_only(c, tid, ["gemini"])
    assert fleet.reserve("gemini") and not fleet.reserve("gemini")
    try:
        t = run(fleet, tid)
    finally:
        fleet.release("gemini")
    assert t["status"] == "approved" and calls("gemini") == [] and calls("claude") == [] and calls("copilot") == []
    assert t["wait_until"] <= time.time() + fleet.BUSY_WAIT_S + 1
    assert fleet.answering() == set()
    t = run(fleet, tid)                                   # Gemini free again: it answers
    assert t["by"] == "gemini" and fleet.answering() == set()


def test_claude_tasks_run_while_a_long_gemini_call_is_going(env):
    """2026-11-01, owner: fleet tasks all seemed locked to Gemini. The paid loop ran
    one call at a time, design specs (Gemini only, priority 1) first, so Claude's
    tasks waited behind every Gemini call. Each paid AI now has its own lane."""
    import threading
    fleet, mode, calls, policy = env
    policy(lambda p: p["frontier"]["gemini"].update(timeout=8))
    mode("gemini", "slow")                                # a 5-second answer
    slow = fleet.submit("hard", "spec")
    with fleet.conn() as c:
        fleet.set_only(c, slow, ["gemini"])
        c.execute("UPDATE tasks SET status='approved' WHERE id=?", (slow,))
    quick = fleet.submit("hard", "plan")
    with fleet.conn() as c:
        c.execute("UPDATE tasks SET status='approved' WHERE id=?", (quick,))
    stop = threading.Event()
    loop = threading.Thread(target=fleet.frontier_loop, args=(stop,), daemon=True)
    loop.start()
    try:
        # Wait for the Gemini call to start (a busy CI runner can take a moment),
        # then Claude's task must finish while that 5-second call is still going.
        deadline = time.time() + 10
        while time.time() < deadline and "gemini" not in fleet.answering():
            time.sleep(0.05)
        assert "gemini" in fleet.answering(), "the Gemini call never started"
        deadline = time.time() + 4                        # well before the Gemini call can end
        while time.time() < deadline and task(fleet, quick)["status"] != "done":
            time.sleep(0.1)
        assert task(fleet, quick)["status"] == "done" and task(fleet, quick)["by"] == "claude"
        assert "gemini" in fleet.answering()              # still on its own call
    finally:
        stop.set()
        loop.join(timeout=10)


def test_paid_results_are_learned_and_a_failing_ai_is_hidden_and_tried_last(env):
    """Owner 2026-11-01: point a task at another AI before it starts; hide the ones
    that fail all the time for that kind; suggest from what's been learned."""
    fleet, mode, calls, _ = env
    for _ in range(3):                                    # gemini fails three 'hard' tasks in a row
        tid = fleet.submit("hard", "x")
        with fleet.conn() as c:
            fleet.set_only(c, tid, ["gemini"])
        mode("gemini", "broken")
        assert run(fleet, tid)["status"] == "failed"
    ok = fleet.submit("hard", "y")
    assert run(fleet, ok)["by"] == "claude"               # claude answers and that's learned too
    stats = fleet.paid_stats("hard")
    assert stats["gemini"] == (3, 0.0) and stats["claude"] == (1, 1.0)
    fresh = fleet.submit("hard", "z")
    opts = {o["name"]: o for o in fleet.route_options(task(fleet, fresh))}
    assert opts["gemini"]["hidden"] and not opts["claude"]["hidden"] and opts["claude"]["recommended"]
    # tried last for this kind even when policy lists it first
    mode("claude", "lock", text=CLAUDE_LIMIT_NEW)
    mode("gemini", "ok")
    assert run(fleet, fresh)["by"] == "gemini"            # still usable when it's the only one left
    # other kinds are unaffected
    assert "gemini" not in fleet.paid_stats("draft")


def test_the_owner_can_bring_a_hidden_ai_back_and_newer_models_get_a_fresh_look(env):
    fleet, mode, calls, policy = env
    for _ in range(3):
        tid = fleet.submit("hard", "x")
        with fleet.conn() as c:
            fleet.set_only(c, tid, ["gemini"])
        mode("gemini", "broken")
        run(fleet, tid)
    t = task(fleet, fleet.submit("hard", "y"))
    assert next(o for o in fleet.route_options(t) if o["name"] == "gemini")["hidden"]
    fleet.paid_reset("gemini", "hard", by="owner@example.com")          # the owner's revert
    assert "gemini" not in fleet.paid_stats("hard")
    assert not next(o for o in fleet.route_options(t) if o["name"] == "gemini")["hidden"]
    assert any(e.get("event") == "paid_reset" and e["by"] == "owner@example.com" for e in fleet.read_ledger())
    with pytest.raises(ValueError, match="no paid AI called"):
        fleet.paid_reset("nope")
    # a new model_version in policy: older results stop counting by themselves
    for _ in range(3):
        tid = fleet.submit("hard", "x")
        with fleet.conn() as c:
            fleet.set_only(c, tid, ["gemini"])
        run(fleet, tid)
    assert fleet.paid_stats("hard")["gemini"][0] == 3
    policy(lambda p: p["frontier"]["gemini"].update(model_version="gemini-4"))
    assert "gemini" not in fleet.paid_stats("hard")
    # and results older than PAID_FORGET_DAYS don't count either
    old = (datetime.now() - timedelta(days=fleet.PAID_FORGET_DAYS + 1)).isoformat(timespec="seconds")
    with open(fleet.STATE / "model-results.jsonl", "a") as f:
        f.write(json.dumps({"at": old, "kind": "hard", "model": "paid:claude", "passed": False}) + "\n")
    assert "claude" not in fleet.paid_stats("hard")


def test_another_ai_can_be_picked_only_before_the_work_starts(env):
    fleet, mode, calls, _ = env
    tid = fleet.submit("hard", "x")
    with fleet.conn() as c:
        fleet.set_only(c, tid, ["gemini"])                                # queued: fine
        c.execute("UPDATE tasks SET status='claimed' WHERE id=?", (tid,))
        with pytest.raises(ValueError, match="before its work starts"):
            fleet.set_only(c, tid, ["claude"])
        fleet.set_only(c, tid, [])                                         # lifting is always allowed
