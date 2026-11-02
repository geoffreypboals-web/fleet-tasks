#!/usr/bin/env bash
# uninstall.sh -- stop fleet-tasks. Keeps policy.json, the queue and the ledger.
set -euo pipefail
systemctl --user disable --now fleet-tasks.service fleet-web.service 2>/dev/null || true
tailscale serve --https=8960 off >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/fleet-tasks.service" "$HOME/.config/systemd/user/fleet-web.service"
systemctl --user daemon-reload
[[ -f /opt/stacks/fleet-tasks/hooks/claude_hook.py ]] && /usr/bin/python3 /opt/stacks/fleet-tasks/hooks/claude_hook.py uninstall >/dev/null || true   # the Claude Code hooks
claude mcp remove -s user fleet >/dev/null 2>&1 || true      # the MCP server, if install.sh --mcp added it
echo "stopped. Left: /opt/stacks/fleet-tasks, ~/.local/state/fleet-tasks"
