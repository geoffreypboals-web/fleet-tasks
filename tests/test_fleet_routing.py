"""Best model for the work (stacks/fleet-tasks/catalog.py and its use in
fleet.py, board WP-152 step 3e): owner-accepted review scores, local first,
never evict, weights, the Opus rule and Copilot staying last."""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    pol = json.loads((HERE / "tests" / "policy.json").read_text())
    pol["frontier"]["gemini"]["enabled"] = True
    (tmp_path / "policy.json").write_text(json.dumps(pol))
    monkeypatch.setenv("FLEET_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("FLEET_PHONE", "off")
    spec = importlib.util.spec_from_file_location("fleet", HERE / "fleet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def accept(fleet, *rows):
    """Propose and accept scores: (model, kind, score)."""
    ids = fleet.CATALOG.propose(fleet.STATE, [{"model": m, "kind": k, "score": s, "source": "https://example.org/r",
                                               "date": "2026-10"} for m, k, s in rows])
    fleet.CATALOG.decide(fleet.STATE, set(ids), True, by="owner")


def task(fleet, tid):
    return fleet.db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


EP = {"name": "gpu-server-gpu01", "model": "qwen3.6:27b", "tier": "large", "loaded": []}


class Calls:
    """subprocess.run stand-in that answers like each CLI and keeps the commands."""

    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)

        class Out:
            returncode, stderr = 0, ""
            stdout = json.dumps({"result": "ok", "response": "ok"})
        return Out


# ---------------- nothing scored: behaviour as before ----------------

def test_without_scores_nothing_changes(fleet, monkeypatch):
    t = task(fleet, fleet.submit("code", "fix it"))
    ep, why = fleet.local_choice(dict(EP), t)
    assert ep["model"] == "qwen3.6:27b" and why == ""
    calls = Calls()
    monkeypatch.setattr(fleet.subprocess, "run", calls)
    tid = fleet.submit("hard", "x")
    fleet.work_frontier(fleet.db(), task(fleet, tid), approved=True)
    cmd = calls.cmds[0]
    assert cmd[cmd.index("--model") + 1] == "sonnet" and task(fleet, tid)["by"] == "claude"
    assert "default model" in task(fleet, tid)["routed"]


def test_proposed_scores_count_only_once_accepted(fleet):
    ids = fleet.CATALOG.propose(fleet.STATE, [{"model": m, "kind": "code", "score": n, "source": "https://aider.chat/x"}
                                              for m, n in (("qwen2.5-coder:14b", 9), ("qwen3.6:27b", 6))])
    t = task(fleet, fleet.submit("code", "fix it"))
    assert fleet.local_choice(dict(EP), t)[0]["model"] == "qwen3.6:27b"
    fleet.CATALOG.decide(fleet.STATE, set(ids), True, by="owner")
    assert fleet.local_choice(dict(EP), t)[0]["model"] == "qwen2.5-coder:14b"     # idle, and 3 points better


# ---------------- local: never evict, switch margin, tiers, context ----------------

def test_a_busy_endpoint_keeps_its_model(fleet):
    accept(fleet, ("qwen2.5-coder:14b", "code", 9), ("qwen3.6:27b", "code", 6))
    t = task(fleet, fleet.submit("code", "fix it"))
    ep, why = fleet.local_choice(dict(EP, loaded=["qwen3.6:27b"]), t)
    assert ep["model"] == "qwen3.6:27b" and "never evicts" in why


def test_switch_only_when_clearly_better(fleet):
    accept(fleet, ("qwen3.6:27b", "code", 7.5), ("qwen2.5-coder:14b", "code", 8.5))
    t = task(fleet, fleet.submit("code", "fix it"))
    ep, why = fleet.local_choice(dict(EP), t)
    assert ep["model"] == "qwen3.6:27b" and "only 1.0 better" in why
    accept(fleet, ("qwen2.5-coder:14b", "code", 9.5))                     # replaces the 8.5
    ep, why = fleet.local_choice(dict(EP), t)
    assert ep["model"] == "qwen2.5-coder:14b" and "endpoint idle" in why


def test_a_smaller_model_needs_evidence_it_beats_the_tasks_tier(fleet):
    accept(fleet, ("qwen2.5-coder:14b", "code", 9))
    t = task(fleet, fleet.submit("code", "fix it"))                       # code starts at large; nothing large scored
    assert fleet.local_choice(dict(EP), t)[0]["model"] == "qwen3.6:27b"
    accept(fleet, ("qwen3.6:27b", "code", 8.5), ("gemma3:27b", "code", 7))
    assert fleet.local_choice(dict(EP), t)[0]["model"] == "qwen3.6:27b"   # beats them, but by less than 2


def test_models_below_the_tasks_tier_or_too_small_are_left_out(fleet):
    accept(fleet, ("llama3.2:3b", "draft", 10), ("llava:13b", "summarize", 10))
    t = task(fleet, fleet.submit("draft", "write"))                      # draft starts at large
    assert fleet.local_choice(dict(EP), t)[0]["model"] == "qwen3.6:27b"
    big = task(fleet, fleet.submit("summarize", "x" * 30000, tier="medium"))   # ~10k tokens: llava has 4k
    assert fleet.local_choice(dict(EP), big)[0]["model"] == "qwen3.6:27b"


def test_local_runs_first_even_when_a_paid_model_scores_higher(fleet):
    accept(fleet, ("claude-opus", "draft", 10), ("qwen3.6:27b", "draft", 6))
    tid = fleet.submit("draft", "write a scene")
    assert fleet.claim(fleet.db(), "large")["id"] == tid                 # still local work


def test_checked_results_are_recorded_and_blended(fleet):
    accept(fleet, ("qwen3.6:27b", "review", 8))
    for i in range(20):
        fleet.CATALOG.record(fleet.STATE, "review", "qwen3.6:27b", i % 2 == 0)
    acc, st = fleet.CATALOG.accepted(fleet.CATALOG.scores(fleet.STATE)), fleet.CATALOG.stats(fleet.STATE)
    f, why = fleet.CATALOG.fit("qwen3.6:27b", "review", acc, st)
    assert f == pytest.approx(0.7 * 8 + 0.3 * 5) and "70/30" in why
    for i in range(80):
        fleet.CATALOG.record(fleet.STATE, "review", "qwen3.6:27b", True)
    f, why = fleet.CATALOG.fit("qwen3.6:27b", "review", acc, fleet.CATALOG.stats(fleet.STATE))
    assert f == pytest.approx(0.5 * 8 + 0.5 * 9) and "50/50" in why


# ---------------- paid: order, model, weight, Opus rule, Copilot last ----------------

def test_paid_order_follows_fit_with_ties_kept_in_policy_order(fleet):
    accept(fleet, ("claude-sonnet", "research", 7), ("gemini-pro", "research", 7.8))
    cat = fleet.CATALOG.load()
    acc, st = fleet.CATALOG.accepted(fleet.CATALOG.scores(fleet.STATE)), {}
    assert fleet.CATALOG.order_paid(cat, ["claude", "gemini"], "research", acc, st) == ["claude", "gemini"]
    accept(fleet, ("gemini-flash", "research", 9))
    acc = fleet.CATALOG.accepted(fleet.CATALOG.scores(fleet.STATE))
    assert fleet.CATALOG.order_paid(cat, ["claude", "gemini"], "research", acc, st) == ["gemini", "claude"]
    assert fleet.providers_for("research")[-1] == "copilot"


def test_copilot_stays_last_whatever_it_scores(fleet):
    accept(fleet, ("copilot-auto", "hard", 10), ("claude-sonnet", "hard", 2))
    assert fleet.gate(task(fleet, fleet.submit("hard", "x")), approved=True) == ("claude", None)


def test_opus_only_for_big_kinds_and_it_counts_three(fleet, monkeypatch):
    accept(fleet, ("claude-opus", "hard", 9), ("claude-sonnet", "hard", 7),
           ("claude-opus", "review", 9), ("claude-sonnet", "review", 7))
    calls = Calls()
    monkeypatch.setattr(fleet.subprocess, "run", calls)
    tid = fleet.submit("hard", "x")
    fleet.work_frontier(fleet.db(), task(fleet, tid), approved=True)
    assert calls.cmds[-1][calls.cmds[-1].index("--model") + 1] == "opus"
    assert fleet.calls_today() == 3 and fleet.calls_today("claude") == 1
    assert "opus (weight 3)" in task(fleet, tid)["routed"]
    tid = fleet.submit("review", "x", tier="frontier")
    fleet.work_frontier(fleet.db(), task(fleet, tid), approved=True)
    assert calls.cmds[-1][calls.cmds[-1].index("--model") + 1] == "sonnet"      # review isn't a big kind


def test_a_heavy_model_is_skipped_when_today_has_no_room(fleet, monkeypatch):
    _cap = json.loads(fleet.POLICY.read_text()); _cap["frontier_daily_total"] = 10
    fleet.POLICY.write_text(json.dumps(_cap))                      # these test the optional all-together cap
    accept(fleet, ("claude-opus", "hard", 9), ("claude-sonnet", "hard", 7))
    for _ in range(8):
        fleet.ledger({"event": "call", "provider": "gemini"})
    calls = Calls()
    monkeypatch.setattr(fleet.subprocess, "run", calls)
    fleet.work_frontier(fleet.db(), task(fleet, fleet.submit("hard", "x")), approved=True)
    assert calls.cmds[-1][calls.cmds[-1].index("--model") + 1] == "sonnet"       # 2 left: Opus needs 3
    assert fleet.calls_today() == 9


def test_a_locked_out_opus_call_gives_back_all_three(fleet, monkeypatch):
    accept(fleet, ("claude-opus", "hard", 9))

    def run(cmd, **kw):
        class Out:
            returncode, stdout, stderr = 1, "", "Claude AI usage limit reached|1900000000"
        if cmd[0] != "claude":
            Out.returncode, Out.stdout = 0, json.dumps({"response": "from gemini"})
        return Out
    monkeypatch.setattr(fleet.subprocess, "run", run)
    tid = fleet.submit("hard", "x")
    fleet.work_frontier(fleet.db(), task(fleet, tid), approved=True)
    assert task(fleet, tid)["by"] == "gemini" and fleet.calls_today() == 1


# ---------------- proposals from a research answer ----------------

def test_parse_proposals_keeps_only_well_formed_rows(fleet):
    cat = fleet.CATALOG.load()
    text = """Here are the scores:
[{"model": "qwen2.5-coder:14b", "kind": "code", "score": 8, "source": "https://aider.chat/docs/leaderboards/", "date": "2026-09"},
 {"model": "gpt-9", "kind": "code", "score": 8, "source": "https://x.org"},
 {"model": "gemma3:27b", "kind": "poetry", "score": 8, "source": "https://x.org"},
 {"model": "gemma3:27b", "kind": "draft", "score": 11, "source": "https://x.org"},
 {"model": "gemma3:27b", "kind": "draft", "score": 6, "source": "javascript:alert(1)"},
 {"model": "gemma3:27b", "kind": "draft", "score": 6, "source": ""}]"""
    good, bad = fleet.CATALOG.parse_proposals(cat, text)
    assert [g["model"] for g in good] == ["qwen2.5-coder:14b"] and len(bad) == 5
    assert any("web address" in b for b in bad)
    assert fleet.CATALOG.parse_proposals(cat, "no list here") == ([], ["no JSON list of scores found in the answer"])


def test_scores_import_and_accept_from_the_terminal(fleet, capsys):
    tid = fleet.submit("research", "scores please")
    c = fleet.db()
    fleet.finish(c, tid, "done", '[{"model": "gemma3:27b", "kind": "draft", "score": 7, "source": "https://e.org/a"}]')
    assert fleet.scores_cli("import", [str(tid)]) == 0 and "1 score(s) proposed" in capsys.readouterr().out
    fleet.scores_cli("list", [])
    assert "proposed" in capsys.readouterr().out
    fleet.scores_cli("accept", ["all"])
    assert "1 accepted" in capsys.readouterr().out
    acc = fleet.CATALOG.accepted(fleet.CATALOG.scores(fleet.STATE))
    assert acc[("gemma3:27b", "draft")]["by"].startswith("cli:")


def test_every_catalogue_model_is_licensed_and_every_paid_one_has_a_provider():
    cat = json.loads((HERE / "catalog.json").read_text())
    reg = (HERE / "MODEL-LICENSES.md").read_text().lower()
    pol = json.loads((HERE / "tests" / "policy.json").read_text())
    for m in cat["models"]:
        if m["cost"] == "local":
            assert m["name"].split(":")[0].lower() in reg, f"{m['name']} isn't in MODEL-LICENSES.md"
        else:
            assert m["provider"] in pol["frontier"]
    for name in pol["frontier"]:
        assert sum(1 for m in cat["models"] if m.get("provider") == name and m.get("default")) == 1


def test_the_approval_card_says_which_model_would_answer(fleet):
    t = task(fleet, fleet.submit("hard", "x"))
    assert fleet.would_use(t) == "claude sonnet"
    accept(fleet, ("claude-opus", "hard", 9))
    assert fleet.would_use(t) == "claude opus, counts 3 calls"
    fleet.lock("claude", fleet.datetime.now() + fleet.timedelta(hours=1), "usage limit")
    assert fleet.would_use(t) == "gemini auto"
