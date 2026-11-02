"""fleet-tasks Backlog tab, builds and autopilot (stacks/fleet-tasks/backlog.py,
owner decision D-2026-10-31-05). A real git board and a real git checkout with
its own origin; Aider, Claude and gh are fakes, so nothing leaves the machine
and no model runs."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FT = ROOT


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed():
    pk = lambda i, **kw: dict({"id": i, "title": f"item {i}", "status": "open", "claim": None,
                               "repos": ["demo"], "est_sessions": 1, "history": []}, **kw)
    return {"title": "t", "lease_hours": 6, "lane_accounts": {"claude-A": "A", "claude-B": "B"},
            "agent_tiers": {"claude-A": "paid-high", "claude-B": "paid-high", "copilot": "paid-mid", "local": "local"},
            "packages": [pk("WP-10", tier="local", lane="local"),
                         pk("WP-11", tier="paid-mid", lane="any"),
                         pk("WP-12", tier="paid-high", lane="A"),
                         pk("H-01", lane="human", repos=[], next_step="Run `docker restart ghr-demo` on gpu-server",
                            steps=["Then check `docker ps`"]),
                         pk("WP-13", depends_on=["H-01"])]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    fleet = load("fleet_for_backlog", FT / "fleet.py")
    bk = load("backlog_under_test", FT / "backlog.py")
    # The board: a bare remote seeded through ai_board's own init.
    board_remote = tmp_path / "board.git"
    git("init", "--bare", "--quiet", str(board_remote), cwd=tmp_path)
    (tmp_path / "seed.json").write_text(json.dumps(seed()))
    bk.BOARD.init_board(tmp_path / "seeder", str(board_remote), str(tmp_path / "seed.json"), "claude-A@test")
    # A checkout with its own origin, like ~/demo on gpu-server.
    origin = tmp_path / "demo.git"
    git("init", "--bare", "--quiet", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "home" / "demo"
    git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    (work / "app.py").write_text("print('helo')\n")
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", "init", cwd=work)
    git("push", "--quiet", "origin", "HEAD:main", cwd=work)
    git("remote", "set-head", "origin", "main", cwd=work)
    monkeypatch.setattr(fleet, "repos", lambda: {"demo": work})
    calls, kws, edits = [], [], {"files": {"app.py": "print('hello')\n"}}

    def run(cmd, **kw):
        """aider/claude: write the planned edit; gh: pretend to open a PR; git: real."""
        calls.append(cmd)
        kws.append(kw)
        if cmd[0] in ("aider", "claude"):
            for name, text in edits["files"].items():
                Path(kw["cwd"], name).parent.mkdir(parents=True, exist_ok=True)
                Path(kw["cwd"], name).write_text(text)
            return subprocess.CompletedProcess(cmd, 0, "done", "")
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/x/demo/pull/7\n", "")
        return subprocess.run(cmd, **kw)

    up = {"local": True}
    b = bk.Backlog(fleet, board_dir=tmp_path / "board", remote=str(board_remote), run=run,
                   probe=lambda url: up["local"])
    return {"kws": kws, "up": up, "b": b, "bk": bk, "fleet": fleet, "calls": calls, "edits": edits, "origin": origin, "work": work}


def board_item(e, wp):
    return e["bk"].BOARD.find(e["b"].board(fresh=True), wp)


def test_state_lists_open_work_in_order_with_best_fit(env):
    s = env["b"].state()
    ids = [i["id"] for i in s["items"]]
    assert "H-01" not in ids and [o["id"] for o in s["owner_tasks"]] == ["H-01"]   # owner tasks in their own list
    fit = {i["id"]: i["fit"] for i in s["items"]}
    assert fit["WP-10"]["to"] == "local" and fit["WP-10"]["auto_ok"]
    assert fit["WP-11"]["to"] == "copilot" and fit["WP-12"]["to"] == "claude-A" and not fit["WP-12"]["auto_ok"]
    assert s["autopilot"] == {"on": False, "today": 0, "max": 4}
    assert "local" in s["routes"] and "claude" in s["builders"]


def test_owner_assigns_and_reorders_on_the_board(env):
    b = env["b"]
    assert b.assign("WP-12", "local") == "route WP-12 -> local"
    item = board_item(env, "WP-12")
    assert item["route"]["by"] == "human-owner@gpu-server" and item["tier"] == "local"
    b.set_order({"labels": {"home-lab": "Home lab"}, "ranks": {"home-lab": 1}, "repos": {"demo": "home-lab"}})
    assert b.state()["items"][0]["product"] == "Home lab"
    with pytest.raises(env["bk"].BOARD.BoardError):
        b.assign("WP-12", "nobody")


def test_plan_queues_a_fleet_draft_task(env):
    tid = env["b"].plan("WP-11")
    with env["fleet"].conn() as c:
        row = c.execute("SELECT kind, prompt FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row[0] == "draft" and "WP-11" in row[1] and "implementation plan" in row[1]


def test_local_build_pushes_a_branch_and_opens_a_draft_pr(env):
    b = env["b"]
    env["edits"]["files"][".aider.chat.history.md"] = "chat log"     # Aider's own files must not be committed
    bid = b.request_build("WP-10")
    assert b.builds()[0]["status"] == "queued" and b.builds()[0]["builder"] == "local"
    assert b.tick_queue("local") == f"build {bid}: pr_open"
    bd = b.builds()[0]
    assert bd["pr"] == "https://github.com/x/demo/pull/7" and bd["branch"] == "fleet/wp-10-1"
    pushed = git("ls-remote", "--heads", str(env["origin"]), cwd=env["work"])
    assert "refs/heads/fleet/wp-10-1" in pushed and "refs/heads/main" in pushed
    files = git("show", "--name-only", "--format=", "fleet/wp-10-1", cwd=env["origin"]).split()
    assert files == ["app.py"]                                          # .aider* refused
    assert git("show", "main:app.py", cwd=env["origin"]) == "print('helo')\n"   # main untouched
    gh = next(c for c in env["calls"] if c[0] == "gh")
    assert "--draft" in gh and gh[gh.index("--base") + 1] == "main"
    item = board_item(env, "WP-10")
    assert item["status"] == "claimed" and item["claim"]["agent"] == "local-fleet@gpu-server"
    assert "draft PR" in item["last_checkpoint"]
    assert not any((env["fleet"].STATE / "worktrees").iterdir())       # worktree cleaned up


def test_paid_build_waits_for_ok_and_runs_claude_without_a_shell(env):
    b = env["b"]
    bid = b.request_build("WP-11")
    assert b.builds()[0]["status"] == "needs_ok"
    assert b.tick_queue("claude") == "off"                              # nothing runs before the OK
    b.approve_build(bid)
    assert b.tick_queue("claude") == f"build {bid}: pr_open"
    claude = next(c for c in env["calls"] if c[0] == "claude")
    assert claude[claude.index("--allowedTools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert "Bash" in claude[claude.index("--disallowedTools") + 1]
    assert board_item(env, "WP-11")["claim"]["agent"] == "claude-A@gpu-server"
    ledger = env["fleet"].read_ledger()
    assert any(e.get("kind") == "build" and e.get("provider") == "claude" for e in ledger)
    with pytest.raises(ValueError, match="isn't waiting"):
        b.approve_build(bid)


def test_too_big_or_secret_changes_are_refused_and_released(env):
    b = env["b"]
    env["edits"]["files"] = {f"f{i}.py": "x = 1\n" for i in range(12)}
    bid = b.request_build("WP-10")
    assert b.tick_queue("local") == f"build {bid}: failed"
    assert "too big" in b.builds()[-1]["note"]
    item = board_item(env, "WP-10")
    assert item["status"] == "open" and "too big" in item["history"][-1]["note"]
    assert "fleet/" not in git("ls-remote", "--heads", str(env["origin"]), cwd=env["work"])
    env["edits"]["files"] = {".env": "TOKEN=x\n"}
    bid = b.request_build("WP-10")
    assert b.tick_queue("local") == f"build {bid}: failed" and "never commits" in b.builds()[-1]["note"]


def test_no_change_is_reported_and_released(env):
    b = env["b"]
    env["edits"]["files"] = {"app.py": "print('helo')\n"}                # same as main
    bid = b.request_build("WP-10")
    assert b.tick_queue("local") == f"build {bid}: no_change"
    assert board_item(env, "WP-10")["status"] == "open"


def test_autopilot_fills_each_queue_in_order_and_obeys_its_switches(env):
    b, fleet = env["b"], env["fleet"]
    assert b.tick() == {"local": "off", "local-01": "off", "claude": "off"}
    b.set_autopilot(True)
    did = b.tick()                                                     # each queue takes its own next item
    assert did["local"].startswith("queued build 1 for WP-10") and did["claude"].startswith("queued build 2 for WP-11")
    assert did["local-01"] == "nothing to build"                       # the one local item is local's already
    assert b.builds()[1]["status"] == "needs_ok"                       # paid: waits for the OK
    assert b.tick() == {"local": "build 1: pr_open", "local-01": "nothing to build", "claude": "busy"}
    (fleet.STATE / "STOP").write_text("x")
    assert b.tick_queue("local") == "stopped"
    (fleet.STATE / "STOP").unlink()
    assert b.tick_queue("local") == "nothing to build"                 # WP-12 is paid-high: never automatic
    b.set_autopilot(False)
    assert b.tick_queue("local") == "off"


def test_an_unavailable_ai_holds_up_only_its_own_queue(env, monkeypatch):
    b, fleet = env["b"], env["fleet"]
    monkeypatch.setattr(fleet, "why_not", lambda name, *a: f"{name} locked until 20:00 (usage limit)")
    paid = b.request_build("WP-11")
    b.approve_build(paid)
    local = b.request_build("WP-10")
    assert b.tick_queue("claude") == "waiting: claude locked until 20:00 (usage limit)"
    assert b.tick_queue("local") == f"build {local}: pr_open"          # local work goes on
    assert b.builds()[0]["status"] == "queued" and "locked" in b.builds()[0]["note"]
    q = {x["name"]: x for x in b.state()["queues"]}
    assert not q["claude"]["available"] and q["claude"]["queued"] == 1 and q["local"]["available"]
    env["up"]["local"] = False                                         # Ollama down: the local queue waits
    env["edits"]["files"] = {"app.py": "print('hi')\n"}
    again = b.request_build("WP-10")
    assert b.tick_queue("local").startswith("waiting: http://127.0.0.1:11436 isn't answering")
    monkeypatch.setattr(fleet, "why_not", lambda *a: None)
    assert b.tick_queue("claude") == f"build {paid}: pr_open"
    assert b.next_queued("local")["id"] == again


def test_policy_can_add_a_queue_for_another_ai(env, monkeypatch):
    b, fleet = env["b"], env["fleet"]
    real = fleet.policy
    monkeypatch.setattr(fleet, "policy", lambda: dict(real(), builders={"copilot": {
        "label": "Copilot CLI", "paid": True, "routes": ["copilot"], "provider": "copilot", "cmd": ["copilot", "-p", "{prompt}"]}}))
    assert b.queue_for("copilot") == "copilot" and b.queue_for("claude-A") == "claude" and b.queue_for("human") is None
    bid = b.request_build("WP-11")                                     # best fit copilot -> its own queue
    assert next(x for x in b.builds() if x["id"] == bid)["builder"] == "copilot"
    assert [q["name"] for q in b.queues()] == ["local", "local-01", "claude", "copilot"]


def test_autopilot_daily_limit(env, monkeypatch):
    b = env["b"]
    real = env["fleet"].policy
    monkeypatch.setattr(env["fleet"], "policy", lambda: dict(real(), autopilot={"max_builds_per_day": 1}))
    b.set_autopilot(True)
    b.tick_queue("local")
    b.tick_queue("local")
    assert b.tick_queue("claude") == "daily limit"


def test_owner_task_and_duplicate_builds_are_refused(env):
    b = env["b"]
    with pytest.raises(ValueError, match="owner task"):
        b.request_build("H-01")
    b.request_build("WP-10")
    with pytest.raises(ValueError, match="already has a build"):
        b.request_build("WP-10")


def test_restart_marks_a_running_build_failed(env):
    b = env["b"]
    bid = b.request_build("WP-10")
    b._update(bid, status="running")
    b.recover()
    assert b.builds()[0]["status"] == "failed" and "restart" in b.builds()[0]["note"]


def test_story_writer_work_is_estimated_in_hours_and_gets_a_long_limit(env):
    b = env["b"]
    b.set_order({"repos": {"demo": "story-writer"}})
    bid = b.request_build("WP-10")
    bd = b.builds()[0]
    assert bd["est_minutes"] == 240 and "Story Writer" in bd["est_source"]
    assert b.stall_after(240) == 240 * 60 * 3                           # flagged after 12 hours, not 30 minutes
    assert b.tick_queue("local") == f"build {bid}: pr_open"
    builder_kw = next(kw for c, kw in zip(env["calls"], env["kws"]) if c[0] == "aider")
    assert builder_kw["timeout"] is None                                # never stopped for taking long
    done = b.builds()[0]
    assert done["started"] and done["finished"]


def test_estimates_learn_from_finished_builds_and_items_can_override(env):
    b, bk = env["b"], env["bk"]
    b.request_build("WP-10")
    b._update(1, status="pr_open", started="2026-10-31T10:00:00Z", finished="2026-10-31T10:50:00Z")
    wp = b.item("WP-10")
    assert b.estimate(wp, "local") == (50.0, "median of 1 recent build(s) of demo")
    assert b.estimate(dict(wp, est_hours=6), "local") == (360.0, "the item's estimate")
    assert b.estimate(wp, "claude") == (20.0, "default estimate")      # no history on that queue
    assert b.stall_after(5) == 1800 and b.stall_after(600) == 600 * 60 * 3   # never flagged sooner than 30 min
    assert bk.took_minutes({"started": "2026-10-31T10:00:00Z"}) is None


def test_queue_shows_when_running_work_ends_and_waiting_work_starts(env):
    b, bk = env["b"], env["bk"]
    now = bk.parse_utc("2026-10-31T12:00:00Z")
    b.clock = lambda: now
    first = b.request_build("WP-10")
    b._update(first, status="running", started="2026-10-31T11:50:00Z", est_minutes=60)
    env["edits"]["files"] = {"x.py": "y = 2\n"}
    second = b.request_build("WP-12", builder="local")
    b._update(second, est_minutes=30)
    plan = b.schedule()
    assert plan[first] == {"start": "2026-10-31T11:50:00Z", "done": "2026-10-31T12:50:00Z", "overdue": False,
                           "stalled": False}
    assert plan[second]["start"] == "2026-10-31T12:50:00Z" and plan[second]["done"] == "2026-10-31T13:20:00Z"
    q = {x["name"]: x for x in b.queues()}
    assert q["local"]["free_at"] == "2026-10-31T13:20:00Z" and q["claude"]["free_at"] is None
    b.clock = lambda: bk.parse_utc("2026-10-31T13:00:00Z")            # past its estimate: overdue, not stopped
    late = b.schedule()[first]
    assert late["overdue"] and not late["stalled"] and late["done"] == "2026-10-31T13:01:00Z"
    assert b.schedule()[second]["start"] == "2026-10-31T13:01:00Z"   # the next one waits for it
    b.clock = lambda: bk.parse_utc("2026-10-31T15:00:00Z")            # 3 x 60 min since its start
    assert b.schedule()[first]["stalled"] and b.queues()[0]["stalled"]
    assert b.builds()[0]["status"] == "running"                        # flagged, never stopped


def test_owner_tasks_show_commands_and_close_with_an_answer(env):
    b, BOARD = env["b"], env["bk"].BOARD
    task = b.state()["owner_tasks"][0]
    assert task["commands"] == ["docker restart ghr-demo", "docker ps"] and task["unblocks"] == ["WP-13"]
    with pytest.raises(BOARD.BoardError, match="why it's denied"):
        b.owner_answer("H-01", "denied", "")
    with pytest.raises(BOARD.BoardError, match="isn't an owner task"):
        b.owner_answer("WP-10", "done")
    assert b.owner_answer("H-01", "done", "restarted, runner picked up jobs") == "owner done it H-01"
    item = board_item(env, "H-01")
    assert item["status"] == "verified" and item["owner_answer"]["comment"] == "restarted, runner picked up jobs"
    assert item["history"][-1]["event"] == "owner done"
    s = b.state()
    assert s["owner_tasks"] == [] and "WP-13" in [i["id"] for i in s["items"]]
    with pytest.raises(BOARD.BoardError, match="already verified"):
        b.owner_answer("H-01", "approved")


def test_the_story_writer_pair_builder_steps_aside_and_shares_local_work(env, tmp_path, monkeypatch):
    """Owner 2026-11-01: Story Writer's GPU pair sat idle for a day. A second free builder
    (local-01, pair01 / :11434) takes local items, but never while Story Writer uses the pair."""
    b, fleet = env["b"], env["fleet"]
    loaded, jobs = {"models": []}, {"list": []}
    b.ollama_ps = lambda base: loaded["models"]
    b.activity = lambda url: jobs["list"]
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    lease = load("lease_for_backlog", FT / "gpu_lease.py")
    monkeypatch.setattr(fleet, "LEASE", lease, raising=False)
    assert b.queues_for("local") == ["local", "local-01"] and b.unavailable("local-01") is None
    loaded["models"] = ["gemma3:27b"]                                   # Story Writer's model on the pair
    assert "gemma3:27b is loaded" in b.unavailable("local-01")
    loaded["models"] = ["qwen3.6:27b", "nomic-embed-text"]              # ours (and the embedder) are fine
    assert b.unavailable("local-01") is None
    jobs["list"] = [{"book_id": "book-7", "endpoint": "http://127.0.0.1:11434/api/chat", "status": "running"}]
    assert "Story Writer is running book-7" in b.unavailable("local-01")
    jobs["list"] = [{"book_id": "book-7", "endpoint": "http://127.0.0.1:11436/api/chat", "status": "running"}]
    assert b.unavailable("local-01") is None                           # a book on the other pair doesn't count
    lease.want("pair01", "ai-story-writer")
    assert "ai-story-writer wants these GPUs" in b.unavailable("local-01")
    lease.unwant("pair01", "ai-story-writer")
    # local is busy with a build: the next local item goes to local-01
    first = b.request_build("WP-10")
    b._update(first, status="running")
    assert b.queue_for("local") == "local-01"                          # least loaded of the two free ones
    loaded["models"] = ["gemma3:27b"]
    assert b.queue_for("local") == "local"                             # an unavailable queue is picked last


def test_a_running_build_on_the_pair_stops_when_story_writer_needs_it(env, monkeypatch):
    b = env["b"]
    loaded = {"models": []}
    b.ollama_ps = lambda base: loaded["models"]
    b.activity = lambda url: []
    b.YIELD_CHECK_S = 0.01

    class Proc:
        returncode, ended = None, False

        def __init__(self, cmd, **kw):
            Proc.last = self

        def communicate(self, timeout=None):
            if self.ended:
                return "", ""
            loaded["models"] = ["gemma3:27b"]                           # Story Writer starts mid-build
            raise subprocess.TimeoutExpired("aider", timeout)

        def terminate(self):
            self.ended, self.returncode = True, -15

        def kill(self):
            self.ended = True

    b.popen = Proc
    bid = b.request_build("WP-10", builder="local-01")
    assert b.tick_queue("local-01") == f"build {bid}: queued"
    bd = next(x for x in b.builds() if x["id"] == bid)
    assert bd["status"] == "queued" and "stepped aside: gemma3:27b is loaded" in bd["note"] and Proc.last.ended
    assert board_item(env, "WP-10")["status"] == "open"               # claim released for later
    assert b.tick_queue("local-01").startswith("waiting: gemma3:27b is loaded")
    loaded["models"] = []
    b.popen = subprocess.Popen
    b.run_build = lambda bd: "pr_open"
    assert b.tick_queue("local-01") == f"build {bid}: pr_open"         # runs again once the GPUs are free
