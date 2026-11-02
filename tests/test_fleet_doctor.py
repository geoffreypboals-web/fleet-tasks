"""fleet-tasks requirements (stacks/fleet-tasks/REQUIREMENTS.md and doctor.py):
every outside command the code runs is documented, and doctor.py fails an
install when something required is missing. No network: endpoints point at
a closed port."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

LIBENV = {k: v for k, v in os.environ.items() if k == "LD_LIBRARY_PATH"}  # the runner's Python needs its libpython

HERE = Path(__file__).resolve().parent.parent
DOCTOR = HERE / "doctor.py"


def commands_the_code_runs():
    """First word of every subprocess call and shell line in fleet-tasks."""
    found = set()
    for f in [*HERE.glob("*.py"), *HERE.glob("node/*.py"), *HERE.glob("hooks/*.py")]:
        for m in re.finditer(r'subprocess\.run\(\s*\[\s*"([\w.-]+)"', f.read_text()):
            found.add(m.group(1))
        for m in re.finditer(r'QUERY\s*=\s*\[\s*"([\w.-]+)"', f.read_text()):
            found.add(m.group(1))
    for f in (HERE / "install.sh", HERE / "uninstall.sh"):
        for line in f.read_text().splitlines():
            m = re.match(r"\s*(?:if \[\[.*\]\]; then\s*)?([a-z][\w.-]+)\s", line)
            if m and m.group(1) in {"systemctl", "tailscale", "claude", "python3", "install", "mkdir"}:
                found.add(m.group(1))
    pol = json.loads((HERE / "tests" / "policy.json").read_text())
    for prov in pol["frontier"].values():
        cmd = [c for c in prov["cmd"] if c != "env" and "=" not in c and not c.startswith("-")]
        found.add(Path(cmd[0]).name)
    return found - {"install", "mkdir", "python3"}       # coreutils / Python itself


def test_every_command_is_in_requirements():
    req = REQUIREMENTS = (HERE / "REQUIREMENTS.md").read_text()
    cmds = commands_the_code_runs()
    assert {"git", "gh", "tailscale", "systemctl", "claude", "nvidia-smi"} <= cmds
    names = {"systemctl": "systemd", "nvidia-smi": "nvidia-smi", "gh": "GitHub CLI"}
    missing = [c for c in cmds if names.get(c, c) not in req]
    assert not missing, f"add these to REQUIREMENTS.md and doctor.py: {missing}"
    assert REQUIREMENTS


def run(tmp_path, *args, policy=None):
    pol = tmp_path / "policy.json"
    if policy is not None:
        pol.write_text(json.dumps(policy))
    env = {"FLEET_POLICY": str(pol), "FLEET_STATE": str(tmp_path / "state"), **LIBENV, "PATH": "/usr/bin:/bin",
           "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, str(DOCTOR), *args], capture_output=True, text=True, timeout=60, env=env,
                          check=False)


def test_install_fails_without_a_policy(tmp_path):
    r = run(tmp_path, "--install")
    assert r.returncode == 1 and "MISSING" in r.stdout and "policy.json" in r.stdout


def test_json_report_and_warnings_dont_fail(tmp_path):
    pol = {"endpoints": [{"name": "nowhere", "url": "http://127.0.0.1:9", "model": "m", "tier": "small"}],
           "frontier": {"gemini": {"enabled": False, "cmd": ["no-such-gemini-cli"]}}}
    r = run(tmp_path, "--json", policy=pol)
    rows = {x["what"]: x for x in json.loads(r.stdout)}
    assert rows["Ollama nowhere"]["status"] == "warn"
    assert rows["paid AI: no-such-gemini-cli"]["status"] == "warn"         # switched off: never required
    assert "switched off" in rows["paid AI: no-such-gemini-cli"]["detail"]
    assert rows["policy.json"]["status"] == "ok"


def test_an_enabled_paid_cli_that_is_missing_fails_install(tmp_path):
    pol = {"endpoints": [{"name": "nowhere", "url": "http://127.0.0.1:9", "model": "m", "tier": "small"}],
           "frontier": {"claude": {"enabled": True, "cmd": ["env", "-C", "/tmp", "A=1", "no-such-claude"]}}}
    r = run(tmp_path, "--install", "--json", policy=pol)
    rows = {x["what"]: x for x in json.loads(r.stdout)}
    assert rows["paid AI: no-such-claude"]["status"] == "MISSING" and r.returncode == 1


def _doctor():
    import importlib.util
    spec = importlib.util.spec_from_file_location("doctor_tools", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_list_is_complete_and_says_each_licence():
    """tools.json (owner 2026-10-28: 'set a dependency list for the tools'): every entry
    says what it's for, how it's checked, how to get it, and whether it can be used commercially."""
    data = json.loads((HERE / "tools.json").read_text())
    types = set(data["_check_types"])
    ids = [t["id"] for t in data["tools"]]
    assert len(ids) == len(set(ids))
    for t in data["tools"]:
        for key in ("id", "name", "kind", "used_for", "check", "install", "licence", "commercial"):
            assert t.get(key), f"{t.get('id')}: no {key}"
        assert t["check"]["type"] in types, t["id"]
        assert t["commercial"] in ("yes", "conditions", "no"), t["id"]
        if t["commercial"] != "yes":
            assert t.get("licence_note"), f"{t['id']}: say what the condition is"
        if t["check"]["type"] == "inside":
            assert t["check"]["of"] in ids
    text = json.dumps(data)
    for private in ("your-tailnet", "100.64.0.1"):     # publishable: nothing site-specific
        assert private not in text, private


def test_requirements_table_matches_the_tools_list():
    req = (HERE / "REQUIREMENTS.md").read_text()
    table = req.split("<!-- tools.json table: made by doctor.py --tools-md; don't edit by hand -->\n")[1]
    table = table.split("\n<!-- end of tools.json table -->")[0]
    assert table == _doctor().tools_markdown(), "run: python3 doctor.py --tools-md, and paste it into REQUIREMENTS.md"


def test_each_kind_of_tool_check(tmp_path, monkeypatch):
    d = _doctor()
    monkeypatch.setattr(d, "_get", lambda url, timeout=3: {"ok": True, "openscad_available": True,
                                                              "blender_available": False})
    seen = {}
    svc = {"id": "svc", "check": {"type": "http_json", "default_url": "http://x:1", "path": "/health",
                                   "fields": ["openscad_available", "blender_available"]}}
    st, detail = d.check_tool(svc, {"tools": {"svc": {"url": "http://y:2"}}}, seen)
    assert st == d.WARN and "http://y:2" in detail and "blender_available" in detail      # the site's address wins
    assert d.check_tool({"id": "o", "check": {"type": "inside", "of": "svc", "field": "openscad_available"}},
                        {}, seen)[0] == d.OK
    assert d.check_tool({"id": "b", "check": {"type": "inside", "of": "svc", "field": "blender_available"}},
                        {}, seen)[0] == d.WARN
    assert d.check_tool({"id": "p", "check": {"type": "program", "cmd": "sh", "version_args": ["-c", "echo 1"]}},
                        {}, {})[0] == d.OK
    assert d.check_tool({"id": "p", "check": {"type": "program", "cmd": "no-such-tool-here"}}, {}, {})[0] == d.WARN
    assert d.check_tool({"id": "v", "check": {"type": "venv", "python": sys.executable, "imports": ["json"]}},
                        {}, {})[0] == d.OK
    assert d.check_tool({"id": "v", "check": {"type": "venv", "python": str(tmp_path / "none"), "imports": []}},
                        {}, {})[0] == d.WARN
    monkeypatch.setenv("SOME_TOKEN", "x")
    assert d.check_tool({"id": "s", "check": {"type": "secret", "service": "s", "name": "SOME_TOKEN"}}, {}, {})[0] == d.OK
    monkeypatch.setattr(d, "_get", lambda url, timeout=3: {"models": [{"name": "gemma3:12b"}]})
    assert d.check_tool({"id": "m", "check": {"type": "ollama_model", "models": ["gemma3:12b"]}},
                        {"endpoints": [{"name": "a", "url": "http://a"}]}, {})[0] == d.OK
    assert d.check_tool({"id": "x", "check": {"type": "manual", "how": "look"}}, {}, {}) == (d.WARN, "check by hand: look")


def test_an_optional_tool_never_fails_the_install_check(monkeypatch):
    d = _doctor()
    monkeypatch.setattr(d, "load_tools", lambda: [{"id": "gone", "name": "Gone", "kind": "program", "used_for": ["x"],
                                                   "check": {"type": "program", "cmd": "no-such-tool-here"},
                                                   "install": "get it", "licence": "MIT", "commercial": "yes"}])
    rows = d.tool_checks({})
    assert rows[0][0] == d.WARN and rows[0][1] == "tool: Gone"
