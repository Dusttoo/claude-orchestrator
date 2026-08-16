---
description: Bootstrap the orchestration harness in the current repo (scaffold configuration, wire hooks, check project acceptance criteria).
---

Set up the orchestration harness in THIS repository. Be conservative: detect,
propose, confirm before writing.

1. **Detect the stack.**
   - Branches: `git branch -r` -> infer the repository's configured branch roles.
   - CI check names: read `.github/workflows/*.yml` -> the `name:` of each job
     that gates merges (e.g. TypeScript, Vitest, Build, Playwright).
   - Pre-commit commands: read `package.json` scripts (typecheck/build/test) or
     the equivalent for the repo's language.
   - Ticket system: look for a tracker convention in branch names, recent
     commits, or repo docs. Default to `none` if unclear.

2. **Scaffold `.orchestration/config.yaml`** from the plugin's
   `templates/config.yaml`, filled in with what you detected. Show the user the
   filled config and let them correct it before writing.
   After writing, validate it:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py validate-config
   ```

3. **Project contract.** Confirm a `CLAUDE.md` (and ideally `AGENTS.md`) exists at the
   repo root -- the gate agents read it for the actual rules. If missing, offer
   to generate a starter (project summary, stack, hard rules, git workflow) and
   tell the user this is where the repo's KNOWLEDGE accrues over time. The plugin
   provides discipline; CLAUDE.md provides knowledge.

4. **Wire the hooks.** Add the plugin's hooks to `.claude/settings.json` (or
   confirm the plugin's `hooks/hooks.json` is already active via the plugin):
   - PreToolUse on Bash -> the merge-guard.
   - Stop -> the worktree sweep.
   Gitignore `.orchestration/.gate-status/` and the worktree base.

5. **Branch protection (optional, recommended).** Offer to apply protection via
   `gh api`: required status checks on both branches (strict), no direct pushes,
   admin enforcement on production. Show the payload first; only apply on
   confirmation.

6. **Smoke and conformance test.** Validate the target configuration and smoke
   the hooks in this repo. Run
   `${CLAUDE_PLUGIN_ROOT}/scripts/run-plugin-conformance.sh` for the reusable
   merge-guard and worktree-cleanup suites. These tests remain in the plugin;
   never copy them (or plugin scripts/process docs) into the target repository.

Report what was created/changed and the one manual step left (usually: review
the generated config + CLAUDE.md).
