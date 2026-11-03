# Instructions for AI agents

Shared by every AI tool that works on this repo (Claude Code, Copilot, Gemini, Codex, the local models). This repo's own coding conventions stay where they are (`CLAUDE.md` or the README); this file adds the fleet-wide rule for checking work out.

## Multi-AI work rule (fleet-wide)

Several AIs work on this repo: two Claude accounts, GitHub Copilot, Gemini,
and local models on lambda2. Before changing anything beyond a one-line fix:

1. Check the work out on the fleet board: `ai_board whoami`, `ai_board next`
   or `ai_board show <id>`, then `ai_board claim <id>`. The tool and full
   rules are in local-ai-infrastructure/docs/multi-ai/MULTI-AI-WORK.md.
   Your identity is in AI_BOARD_AGENT (e.g. copilot@winD).
2. Work only in that package's worktree and branch. Don't edit paths
   another claim owns.
3. Checkpoint after every step (`ai_board checkpoint <id> "<done>" --next "<next>"`),
   and stop at ~75% of your usage with a precise --next.
4. Push mechanical work to scripts or the local models. Paid tokens are for
   work that needs them.
5. Finish with `ai_board done <id> --commit <sha> --tests "<result>" --pr <url>`.
   A different agent verifies.
6. Never add a tool, model, voice or asset flagged 🚩 (personal use /
   non-commercial) in local-ai-infrastructure/docs/free-tools/LICENSE-REGISTER.md
   to shipped code.
