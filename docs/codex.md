# Codex plugin support

This repository is a Codex plugin folder. The Codex manifest lives at
`.codex-plugin/plugin.json` and exposes the natural-language skills in `skills/`.

Codex does not ingest the Claude Code slash-command files in `commands/`.
Current Codex hosts can discover lifecycle hooks from `hooks/hooks.json` after
the user reviews and trusts them, but that capability is host/version-dependent.
The plugin never relies on it: natural-language skills call the same controller,
merge, and cleanup scripts used by the Claude Code commands.

## What Codex gets

After install, Codex can invoke these skills by natural language:

| Skill | Purpose |
|---|---|
| `orchestrate-ticket` | Run one ticket end to end: implement, review, security, verify, merge |
| `orchestrate-sprint` | Run or resume a configured Jira sprint with dependency-aware bounded concurrency |
| `gate-pr` | Gate an existing PR and merge only after all green proof exists |
| `release-integration` | Advance a configured release/candidate transition through the shared engine |
| `orchestration-init` | Bootstrap `.orchestration/` config in a target repo |
| `scope-ticket` | Turn a thin ticket into testable acceptance criteria |
| `recover-agent-work` | Recover work from a stopped or interrupted agent worktree |

The skills reference role briefs in `agents/` and scripts in `scripts/`. A Codex
agent should resolve those plugin-relative paths to absolute paths, then run the
scripts with the target repository as the working directory. Configured
workflow policy is planned and enforced through `scripts/orchestration-engine.py`.
Sprint scheduling and recovery are enforced through
`scripts/sprint-controller.py`, the same state machine used by Claude Code's
`/orchestrate-sprint` command. Codex reserves each lane before launching a fresh
per-ticket task, reconciles uncertain running references after a restart, and
continues independent tickets past blockers.

## Hook behavior

When supported, Codex can discover `hooks/hooks.json`. Open `/hooks` to inspect,
trust, disable, or re-enable plugin-bundled hooks. A host that does not expose
them remains supported.

The hook commands use `${CLAUDE_PLUGIN_ROOT}` because Claude Code sets that
variable and Codex sets it for compatibility with existing plugin hooks. Codex
also provides `${PLUGIN_ROOT}` for Codex-specific hooks.

The `PreToolUse` merge guard is inert until the target repository opts into the
harness with `.orchestration/config.yaml`. In uninitialized repositories it
exits successfully without creating `.orchestration/` or blocking commands.
After initialization, it blocks raw `gh pr merge` commands unless a fresh
all-green marker matches the active plugin version and exact PR head/base
identity. `merge-on-green.sh` performs the same validation directly before the
sanctioned merge, so disabling or lacking the hook cannot bypass the gate.

The `Stop` worktree sweep is disabled by the safe default
`worktree_cleanup: manual`. With `auto`, both the skill's explicit post-merge
cleanup and the optional hook remove only clean, unlocked `agent-*` worktrees
under `worktree_base`; dirty and locked worktrees remain recoverable.

## Local install

Codex installs plugins from marketplace roots. The default personal marketplace
file is `~/.agents/plugins/marketplace.json`, and its plugin entries resolve
`./plugins/<name>` relative to your home directory.

For local development, clone or symlink this repository to:

```bash
~/plugins/claude-orchestrator
```

Then ensure `~/.agents/plugins/marketplace.json` contains this entry:

```json
{
  "name": "claude-orchestrator",
  "source": {
    "source": "local",
    "path": "./plugins/claude-orchestrator"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Developer Tools"
}
```

If the file does not exist yet, seed it as:

```json
{
  "name": "personal",
  "interface": {
    "displayName": "Personal"
  },
  "plugins": [
    {
      "name": "claude-orchestrator",
      "source": {
        "source": "local",
        "path": "./plugins/claude-orchestrator"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Developer Tools"
    }
  ]
}
```

Install it from the default personal marketplace:

```bash
codex plugin add claude-orchestrator@personal
```

The default personal marketplace is discovered implicitly by Codex; it does not
need `codex plugin marketplace add`.

## Team or Git marketplace

A non-default marketplace root should use this shape:

```text
<marketplace-root>/
  .agents/plugins/marketplace.json
  plugins/claude-orchestrator/
    .codex-plugin/plugin.json
    skills/
    agents/
    scripts/
```

The marketplace entry should point at `./plugins/claude-orchestrator`. Then add
and install from that marketplace:

```bash
codex plugin marketplace add <marketplace-root-or-git-url>
codex plugin add claude-orchestrator@<marketplace-name>
```

Use the `name` field from `.agents/plugins/marketplace.json` as
`<marketplace-name>`.

## Scripted merge path

For Codex and Claude workflows, keep merges on the scripted path:

```bash
scripts/merge-guard.sh --record-green <pr> [result_file]
scripts/merge-on-green.sh <pr> <branch> all-green <verify_path>
```

The hook is an optional local guardrail around agent tool calls. The sanctioned
scripted path enforces the same proof without it. Branch protection remains
the required out-of-band backstop for direct pushes, GitHub UI merges, or any
shell that does not run through trusted hooks.
