---
description: Advance a configured release or candidate workflow through the shared orchestration engine.
argument-hint: <transition-name> [candidate-id]
---

Advance a release workflow using repository configuration. Do not implement
release policy in this command; the transition graph, guards, branch roles,
approvals, artifact identity, environments, tags, and reconciliation rules come
from `.orchestration/config.yaml` and are enforced by the shared engine.

1. **Validate configuration first.**
   ```bash
   scripts/orchestration-engine.py validate-config
   ```
   Unsupported schema versions, missing branch roles, undefined transitions,
   missing adapters, or incomplete guards must stop the run before mutation.

2. **Plan the requested transition.** Use the transition name from `$ARGUMENTS`
   and any candidate/template variables the repository requires:
   ```bash
   scripts/orchestration-engine.py adapter-plan --host claude <transition> \
     --var candidate_id=<id>
   ```
   Treat this plan as authoritative. If the configured workflow has no such
   transition, stop; do not substitute a remembered release sequence.

3. **Collect configured evidence.** For every `required_evidence`,
   `required_ci`, `required_approvals`, `candidate_identity_required`,
   `artifact_identity_required`, and `tag_required` item in the plan, gather the
   corresponding record before attempting the transition. A free-form state file
   edit is not approval.

4. **Execute through the engine.**
   ```bash
   scripts/orchestration-engine.py transition <candidate-id> <transition> \
     --evidence <name>=<path> \
     --ci <category>=green \
     --candidate-sha <sha> \
     --artifact-id <artifact> \
     --tag <tag>
   ```
   Omit arguments only when the plan does not require them.

5. **Legacy compatibility.** Configs without `schema_version: 2` keep the old
   ticket merge scripts and guard behavior, but they do not automatically opt
   into release-candidate states. For a legacy release, follow the repository's
   existing documented process or migrate the config to a schema-versioned
   workflow before using candidate transitions.

Report the transition attempted, evidence records used, approval identities,
candidate/artifact identities, tag, and final state.
