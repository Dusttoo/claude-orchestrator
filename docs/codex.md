# Codex plugin support

This repository is a Codex plugin folder. The Codex manifest lives at
`.codex-plugin/plugin.json` and exposes the natural-language skills in `skills/`.

Codex does not ingest the Claude Code slash-command files in `commands/`.
Enabled Codex plugins can load lifecycle hooks from the default
`hooks/hooks.json` file, so this plugin intentionally keeps its merge guard and
worktree sweep there. Codex skips plugin-bundled hooks until you review and
trust them with `/hooks`.

## What Codex gets

After install, Codex can invoke these skills by natural language:

| Skill | Purpose |
|---|---|
| `orchestrate-ticket` | Run one ticket end to end: implement, review, security, verify, merge |
| `gate-pr` | Gate an existing PR and merge only after all green proof exists |
| `release-integration` | Release integration to production and immediately back-merge |
| `orchestration-init` | Bootstrap `.orchestration/` config in a target repo |
| `scope-ticket` | Turn a thin ticket into testable acceptance criteria |
| `recover-agent-work` | Recover work from a stopped or interrupted agent worktree |

The skills reference role briefs in `agents/` and scripts in `scripts/`. A Codex
agent should resolve those plugin-relative paths to absolute paths, then run the
scripts with the target repository as the working directory.

## Hook behavior

When the plugin is enabled, Codex can discover `hooks/hooks.json` by default.
Open `/hooks` to inspect, trust, disable, or re-enable the plugin-bundled hooks.

The hook commands use `${CLAUDE_PLUGIN_ROOT}` because Claude Code sets that
variable and Codex sets it for compatibility with existing plugin hooks. Codex
also provides `${PLUGIN_ROOT}` for Codex-specific hooks.

The `PreToolUse` merge guard is inert until the target repository opts into the
harness with `.orchestration/config.yaml`. In uninitialized repositories it
exits successfully without creating `.orchestration/` or blocking commands.
After initialization, it blocks raw `gh pr merge` commands unless a fresh
all-green marker exists for the PR head.

The `Stop` worktree sweep only removes clean, unlocked `agent-*` worktrees under
the configured `worktree_base`. Dirty worktrees are preserved for recovery.

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

The hook is a local guardrail around agent tool calls. Branch protection remains
the required out-of-band backstop for direct pushes, GitHub UI merges, or any
shell that does not run through trusted hooks.
