# Porting to a new repo

The harness is built so that adopting it in a new codebase is a configuration
task, not a fork. Nothing project-specific lives in the plugin; it all lives in
two places on the repo side: `.orchestration/config.yaml` (the mechanics) and
the repo's `CLAUDE.md` / `AGENTS.md` (the knowledge the gate agents enforce).

## The fastest path

From inside the target repo, in a Claude Code session with the plugin installed:

```
/orchestration-init
```

It detects the branch model and CI check names, scaffolds
`.orchestration/config.yaml` for you to review, copies in `ORCHESTRATION.md`,
confirms a `CLAUDE.md` exists, and gitignores the runtime marker directory. The
rest of this doc is what that command sets up, for when you want to do it by hand
or understand what it produced.

## Checklist

1. **Copy the config.** `templates/config.yaml` -> `.orchestration/config.yaml`.
   Fill in:
   - `integration_branch` / `production_branch` from your branch model.
   - `merge_to_integration` / `merge_to_production` (`merge` keeps per-PR history
     on the integration branch; `squash` gives one commit per release).
   - `ci_checks_*`: the exact GitHub check-run `name:` values from your workflows.
     These must match exactly, since the gate polls them by name.
   - `self_check`: your pre-review commands. `typecheck` / `build` / `unit` for a
     typical setup, plus any repo convention (a hex-color grep, a lint rule, a
     codegen-drift check) as its own named entry. This is where a repo teaches
     the harness its own hard checks without editing any script.
   - `verification`: only if you have a heavy suite (e2e). Gate each entry to a
     target with `when:` (`production` for a release-only gate). Omit entirely
     otherwise.
   - `security_required_when`: the diff triggers that make the security gate
     mandatory (auth, migrations, payments, whatever your risk surface is).
   - `ticket`: `jira` / `github` / `none`.

2. **Point `rules_docs` at your knowledge files.** Usually `CLAUDE.md` and
   `AGENTS.md`. The gate agents read these to know the actual rules; this is
   where your project's conventions and past-incident lessons accrue. The plugin
   supplies the discipline; these files supply the knowledge, and they are what
   make the harness get smarter about *your* repo over time.

3. **Gitignore the runtime dirs.** Add `.orchestration/.gate-status/` (the marker
   directory the merge-guard writes) and your worktree base if it is inside the
   repo.

4. **Confirm the hooks are active.** The plugin registers the `PreToolUse`
   merge-guard and the `Stop` worktree sweep. If your Claude Code version does
   not auto-activate plugin hooks, add them to `.claude/settings.json` pointing at
   `${CLAUDE_PLUGIN_ROOT}/scripts/merge-guard.sh` and
   `${CLAUDE_PLUGIN_ROOT}/scripts/sweep-agent-worktrees.sh`.

5. **Branch protection (recommended).** The merge-guard governs the agent shell;
   branch protection closes the paths it cannot see (direct pushes, UI merges).
   Apply required status checks (strict) on both branches, disallow direct
   pushes, and enforce admin on production. `/orchestration-init` can show you the
   `gh api` payloads.

6. **Smoke-test.** From the repo root:
   ```
   bash <plugin>/scripts/preflight.sh
   ```
   It checks git, `gh` auth, the integration branch, and (only if present)
   language-specific tooling. Then run one small ticket through `/orchestrate` end
   to end and confirm the guard actually blocks a bare `gh pr merge` before the
   gates record a marker.

## What is repo-specific vs harness-generic

| Repo-specific (you provide) | Harness-generic (the plugin provides) |
|---|---|
| Branch names, CI check names, merge strategy | The pipeline sequence and the gate logic |
| The self-check and verification commands | The runner that executes them and reports |
| The rules in `CLAUDE.md` / `AGENTS.md` | The agents that read and enforce those rules |
| Which diffs need a security review | The security-review agent and its checklist |

## Language-agnostic notes

The harness assumes only git and the `gh` CLI. `preflight.sh` checks Node tooling
only when a `package.json` is present and Playwright only when a Playwright config
is present, so a non-Node repo is not forced into either. `self_check` and
`verification` commands are whatever your stack uses; the harness does not care
what language they run.
