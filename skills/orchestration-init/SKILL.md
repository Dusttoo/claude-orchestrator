---
name: orchestration-init
description: Bootstrap the orchestration harness in a target repo by creating configuration, checking project acceptance criteria, and wiring plugin-owned guardrails. Use when the user asks to initialize, install, bootstrap, or set up orchestration in a repository, including the equivalent of the Claude /orchestration-init command.
---

# Initialize orchestration in a target repo

Set up the orchestration harness in the current repository. Be conservative:
detect the repo's mechanics, show the inferred config, and get confirmation
before writing repo files when running interactively.

## Plugin paths

The templates and scripts below live in this plugin, not necessarily in the
target repository. Resolve these paths from this skill file before reading or
executing them:

- `../../templates/config.yaml`
- `../../hooks/hooks.json`
- `../../scripts/orchestration-engine.py`
- `../../scripts/preflight.sh`
- `../../scripts/merge-guard.sh`
- `../../scripts/sweep-agent-worktrees.sh`
- `../../scripts/run-plugin-conformance.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

1. Detect the target repo's stack:
   - branches from `git branch -r`, inferring configured branch roles
   - CI check names from `.github/workflows/*.yml`
   - self-check commands from `package.json` or the repo's language equivalent
   - ticket system from branch names, recent commits, or repo docs, defaulting
     to `none` when unclear
2. Scaffold `.orchestration/config.yaml` from `templates/config.yaml`, filled
   with the detected values. Show the filled config and let the user correct
   branch names, checks, or ticket settings before writing. After writing, run
   `orchestration-engine.py validate-config`.
3. Confirm a repo rules document exists. Prefer both `CLAUDE.md` and `AGENTS.md`
   in `rules_docs`; if missing, offer to create a starter so the project's real
   conventions have a place to live.
4. Gitignore `.orchestration/.gate-status/` and the configured worktree base.
5. Wire guardrails according to the host:
   - Claude Code: add `hooks/hooks.json` entries to `.claude/settings.json` if
     plugin hooks are not already active.
   - Codex: Codex plugins expose the skills and scripts, but this manifest does
     not auto-register Claude-style hooks. Keep using `merge-on-green.sh` for
     merges and rely on branch protection for out-of-band enforcement.
6. Optionally propose branch protection via `gh api`: strict required status
   checks, no direct pushes, and admin enforcement on production. Show the
   payload before applying it.
7. Validate config and smoke-test the hooks in the target repo. Then run
   `run-plugin-conformance.sh`, which exercises the reusable merge-guard and
   worktree-cleanup suites from the plugin itself. Do not copy plugin tests,
   scripts, or process docs into the target repository. The target owns only its
   configuration, rules/acceptance criteria, and project-specific tests.

Report the files created or changed and any manual review left, especially
config values and rules-doc content.
