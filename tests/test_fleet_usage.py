"""usage.py: each paid AI's plan usage, read without asking any AI (owner 2026-10-28)."""
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "usage.py"

GH = {"copilot_plan": "individual_pro", "quota_reset_date_utc": "2026-11-01T00:00:00.000Z",
      "quota_snapshots": {"premium_interactions": {"entitlement": 7000, "remaining": 5827, "credits_used": 1172,
                                                   "percent_remaining": 83.2, "unlimited": False,
                                                   "overage_permitted": True, "overage_entitlement": 2000,
                                                   "overage_count": 0}}}


@pytest.fixture()
def usage(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FLEET_CLAUDE_LOGS", str(tmp_path / "logs"))
    spec = importlib.util.spec_from_file_location("fleet_usage", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gh(out, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_copilot_reads_githubs_count_and_flags_overage(usage):
    calls = []
    cp = usage.copilot(gh(json.dumps(GH), calls))
    assert calls == [["gh", "api", "/copilot_internal/user"]]          # a GitHub call, not a Copilot request
    assert cp == {"plan": "individual_pro", "used": 1172, "of": 7000, "left_pct": 83.2, "resets": "2026-11-01",
                  "overage_on": True, "overage_max": 2000, "overage_used": 0}
    assert "OVERAGE ON" in usage.lines({"copilot": cp})[0]


def test_copilot_change_on_githubs_side_shows_unknown(usage):
    assert "error" in usage.copilot(gh("not json"))
    assert "error" in usage.copilot(gh(json.dumps({"copilot_plan": "x"})))
    def missing(cmd, **kw):
        raise FileNotFoundError("gh")
    assert "error" in usage.copilot(missing)


def line(ts, mid, out=10, inp=5, cache=100):
    return json.dumps({"timestamp": ts, "requestId": "r" + mid, "message": {"id": mid, "usage": {
        "input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cache}}})


def test_claude_counts_this_machines_logs_once_per_reply(usage, tmp_path):
    logs = tmp_path / "logs" / "proj"
    logs.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (logs / "a.jsonl").write_text("\n".join([line(now, "m1"), line(now, "m1"), line(now, "m2", out=20),
                                             '{"type": "user"}', "broken {"]) + "\n")
    cache = {}
    got = usage.claude(cache=cache)
    assert got["today"] == {"turns": 2, "tokens": 40, "cache_read": 200} and got["month"]["turns"] == 2
    assert list(cache) == [str(logs / "a.jsonl")]
    # an unchanged file comes from the cache, not a read
    usage._claude_file = lambda p: pytest.fail("re-read an unchanged log")
    assert usage.claude(cache=cache) == got


def test_snapshot_is_cached_for_15_minutes(usage, tmp_path):
    calls = []
    first = usage.snapshot(run=gh(json.dumps(GH), calls))
    assert first["copilot"]["used"] == 1172 and first["claude"]["today"]["turns"] == 0
    assert usage.snapshot(run=gh(json.dumps(GH), calls)) == first and len(calls) == 1
    usage.snapshot(refresh=True, run=gh(json.dumps(GH), calls))
    assert len(calls) == 2
    assert "files" not in first and (tmp_path / "state" / "usage.json").exists()
