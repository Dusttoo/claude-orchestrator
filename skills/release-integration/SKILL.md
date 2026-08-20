---
name: release-integration
description: Advance a configured release or candidate workflow through the shared orchestration engine. The skill name is retained for compatibility; release policy comes from repository configuration, not from this skill.
---

# Advance a configured release workflow

Use the repository's schema-versioned workflow configuration. This skill is the
natural-language entry point for the same behavior exposed by the Claude
`/release` command. Do not independently implement release policy here.

## Plugin paths

Resolve these paths from this skill file before executing them:

- `../../scripts/orchestration-engine.py`
- `../../scripts/merge-guard.sh`
- `../../scripts/merge-command-classifier.py`
- `../../scripts/merge-on-green.sh`
- `../../scripts/run-verification.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

1. Run `orchestration-engine.py validate-config`. Stop before mutation on any
   unsupported schema version, undefined branch role, invalid transition, missing
   adapter, or incomplete guard.
2. Determine the requested transition and candidate identity from the user or the
   repository's current release state.
3. Run:
   ```bash
   orchestration-engine.py adapter-plan --host codex <transition> \
     --var candidate_id=<id>
   ```
   The output is authoritative for branch roles, evidence, approvals, CI
   categories, environment roles, artifact identity, tags, reconciliation, and
   cleanup.
4. Collect each configured evidence item and approval record. Approval must be a
   separate verifiable record, created by the engine or by a configured external
   adapter; never treat a free-form state edit as approval.
5. Execute the transition through:
   ```bash
   orchestration-engine.py transition <candidate-id> <transition> \
     --evidence <name>=<path> \
     --ci <category>=green \
     --candidate-sha <sha> \
     --artifact-id <artifact> \
     --tag <tag>
   ```
   Pass only arguments required by the configured plan.
6. For legacy configs without `schema_version: 2`, do not invent candidate
   states. Continue using the repository's existing release process or migrate
   to a schema-versioned workflow before using release-candidate transitions.

Report the transition, final state, candidate identity, artifact identity,
approval records, evidence paths, and any configured reconciliation or cleanup
work that remains.
