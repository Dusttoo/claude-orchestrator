# Codex plugin support

This repository is a Codex plugin folder. The Codex manifest lives at
`.codex-plugin/plugin.json` and exposes the natural-language skills in `skills/`.

Codex does not ingest the Claude Code slash-command files in `commands/` or
auto-register the Claude hook file in `hooks/hooks.json` from this manifest. The
Codex path uses skills plus the same shell scripts and role briefs.

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

## Guardrail caveat

Claude Code can activate `hooks/hooks.json` as a `PreToolUse` merge guard and a
`Stop` worktree sweep. Codex plugin manifests expose this repository's skills and
scripts, but they do not auto-register that Claude hook file.

For Codex workflows, keep merges on the scripted path:

```bash
scripts/merge-guard.sh --record-green <pr> [result_file]
scripts/merge-on-green.sh <pr> <branch> all-green <verify_path>
```

Branch protection remains the required out-of-band backstop for direct pushes,
GitHub UI merges, or any shell that does not run through the scripted path.
