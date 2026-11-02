#!/usr/bin/env bash
# install.sh -- install or refresh fleet-tasks. Idempotent. policy.json is copied only the
# first time (local edits survive); --policy overwrites it with the repo copy.
# --mcp also registers the MCP server (mcp.py) for Claude Code sessions, user scope.
# --hooks adds the safety and fleet-hint hooks to ~/.claude/settings.json (backup first);
# --usage with it also counts Claude turns per day for the page.
# The web page (web.py, fleet-web.service) needs the logins allowed to open it:
#   FLEET_ALLOWED_LOGINS=you@example.com ./install.sh     (kept in ~/.config/fleet-tasks/web.env)
# or, with no Tailscale (the page then opens only from this machine or an SSH tunnel):
#   FLEET_LOCAL_LOGIN=me ./install.sh
# policy.json: this folder's policy.json, or policy.example.json when there's none.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
SRC_POLICY="$SRC/policy.json"; [[ -f $SRC_POLICY ]] || SRC_POLICY="$SRC/policy.example.json"
STACK=/opt/stacks/fleet-tasks U="$HOME/.config/systemd/user"
POLICY=0 MCP=0 HOOKS=0 USAGE=""
for arg in "$@"; do
  case "$arg" in
    --policy) POLICY=1 ;;
    --mcp) MCP=1 ;;
    --hooks) HOOKS=1 ;;
    --usage) USAGE=--usage ;;
    *) echo "unknown option $arg (use --policy, --mcp, --hooks, --usage)" >&2; exit 2 ;;
  esac
done
# Stop before changing anything if this machine lacks something required (REQUIREMENTS.md).
DOCTOR_ARGS=(--install); [[ $MCP == 1 ]] && DOCTOR_ARGS+=(--mcp); [[ $HOOKS == 1 ]] && DOCTOR_ARGS+=(--hooks)
FLEET_POLICY="$( [[ -f $STACK/policy.json && $POLICY == 0 ]] && echo "$STACK/policy.json" || echo "$SRC_POLICY")" \
  python3 "$SRC/doctor.py" "${DOCTOR_ARGS[@]}" || { echo "install stopped: fix the MISSING items above (see REQUIREMENTS.md)" >&2; exit 1; }
mkdir -p "$STACK" "$U"
install -m 755 "$SRC"/{fleet.py,web.py,mcp.py,top.py,doctor.py,uninstall.sh} "$STACK/"
install -m 644 "$SRC/web.html" "$SRC/gpu_lease.py" "$SRC/memory.py" "$SRC/screen.py" "$SRC/research.py" "$SRC/catalog.py" "$SRC/doc.py" "$SRC/usage.py" "$SRC/design.py" "$SRC/backlog.py" \
  "$SRC/catalog.json" "$SRC/tools.json" "$SRC/REQUIREMENTS.md" "$STACK/"
[[ ! -f "$STACK/policy.json" || $POLICY == 1 ]] && install -m 644 "$SRC_POLICY" "$STACK/policy.json"
mkdir -p "$STACK/hooks" && install -m 755 "$SRC/hooks/claude_hook.py" "$STACK/hooks/"
mkdir -p "$STACK/scripts" && install -m 644 "$SRC/scripts/ai_board.py" "$STACK/scripts/"
install -m 644 "$SRC/fleet-tasks.service" "$SRC/fleet-web.service" "$U/"
CONF="$HOME/.config/fleet-tasks"; mkdir -p "$CONF"
if [[ -n "${FLEET_ALLOWED_LOGINS:-}${FLEET_LOCAL_LOGIN:-}" ]]; then
  { [[ -n "${FLEET_ALLOWED_LOGINS:-}" ]] && printf 'FLEET_ALLOWED_LOGINS=%s\n' "$FLEET_ALLOWED_LOGINS"
    [[ -n "${FLEET_LOCAL_LOGIN:-}" ]] && printf 'FLEET_LOCAL_LOGIN=%s\n' "$FLEET_LOCAL_LOGIN"; true; } > "$CONF/web.env"
  chmod 600 "$CONF/web.env"
elif [[ ! -f "$CONF/web.env" ]]; then
  echo "Set who may open the web page: FLEET_ALLOWED_LOGINS=you@example.com $0 (your tailnet login)," >&2
  echo "or, without Tailscale: FLEET_LOCAL_LOGIN=me $0" >&2; exit 1
fi
systemctl --user daemon-reload
systemctl --user enable --now fleet-tasks.service fleet-web.service
systemctl --user restart fleet-tasks.service fleet-web.service
if grep -q '^FLEET_LOCAL_LOGIN=' "$CONF/web.env" || ! command -v tailscale >/dev/null; then
  echo "installed. Page (local login, no Tailscale): from another computer run"
  echo "  ssh -L 8960:127.0.0.1:8960 $USER@$(hostname)    then open http://127.0.0.1:8960"
else
  tailscale serve --bg --https=8960 http://127.0.0.1:8960 >/dev/null
  echo "installed. Page: https://$(tailscale status --self --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):8960"
fi
if [[ $MCP == 1 ]]; then
  claude mcp remove -s user fleet >/dev/null 2>&1 || true
  claude mcp add -s user fleet -- /usr/bin/python3 "$STACK/mcp.py"
  echo "MCP: 'fleet' added for Claude Code (user scope); new sessions get fleet_submit, fleet_result, ..."
fi
if [[ $HOOKS == 1 ]]; then
  /usr/bin/python3 "$STACK/hooks/claude_hook.py" install $USAGE
fi
echo "Terminal: $STACK/fleet.py nodes"
