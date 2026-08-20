# The merge-guard

The merge-guard turns "never merge on red" from a discipline into a host-neutral
mechanism. `merge-on-green.sh` calls `merge-guard.sh --assert-green` directly
before every sanctioned merge. The same script can also run as a Claude Code or
Codex `PreToolUse` hook to veto raw merge commands when that host supports and
trusts plugin hooks.

## Why a script plus an optional hook

An agent told "do not merge until the gates pass" will, eventually, merge before
the gates pass. Not from malice, from a wrong assumption: a check that returned
empty read as success, a branch protection that was not enforced, a race between
a background CI watcher and the merge command. When that happens at 3am in an
unattended run, an instruction provides no backstop.

The sanctioned script does. It does not matter what the agent believes about the
gate state: the guard re-derives it from the marker, active plugin version, and
live PR head/base identity. A trusted hook adds coverage for raw `gh pr merge`
commands, but correctness does not disappear on a Codex or Claude Code host that
does not register it.

## Hook contract

```
stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
exit 0 : ALLOW  (the entire payload is proven safe non-merge/data syntax)
exit 2 : BLOCK  (a merge or syntax outside the safe subset)
```

The hook is a safe-subset recognizer, not a general Bash interpreter. Its
standard-library-only Python classifier is directly testable and emits a fixed
eight-record result schema. `allow` means the complete payload was proven
harmless within the supported grammar. Both `merge` and `block` deny execution.
Quoted prose, commit/PR bodies, printf/echo payloads, comments, and literal
option values remain data.

Literal merges are recognized across ordinary compound/control boundaries so
diagnostics can identify them, but they are never authorized. Backslash-newline
continuations are normalized before tokenization. Dynamic command names,
globs/brace expansion in executable position, substitutions, heredocs,
here-strings, process substitution, `eval`, `source`, unsupported dispatch and
interpreter forms, malformed executable forms, multiple merges, and resource
limit exhaustion block conservatively. Ordinary literal file redirection for a
safe command remains supported. Complex harmless commands can therefore be
blocked; simplify the payload or run it outside the initialized hook surface.

## What it blocks, and why

1. **Every raw merge.** A marker never authorizes shell text. It authorizes only
   `merge-on-green.sh` after every gate and required CI check passes.

2. **A merge whose recorded identity changed.** A marker binds the authoritative
   current `nameWithOwner`, active plugin version, head branch/SHA, and base
   branch/SHA. A repository mismatch, plugin upgrade, rebase, push, retarget, or
   moved target branch invalidates the proof and requires re-gating.

3. **A merge whose marker is older than the freshness window**
   (`MERGE_GUARD_MAX_AGE_SECONDS`, default 3600). Even when the SHA still
   matches, an hours-old marker means CI state and the base branch have moved on;
   re-gate. This closes the gap where a marker is recorded, the run stalls for
   hours, then a merge fires against a world that has changed.

4. **Unsupported shell syntax.** Unknown execution forms deny by default rather
   than falling through to a partial parser.

## Recording a marker

```
merge-guard.sh --record-green <pr> <result_file>
```

The orchestrator calls this only after all gates PASS and CI is green. The
`result_file` is mandatory. It must be the canonical file emitted by a configured
`run-verification.sh <name>` invocation in the configured gate-status directory,
with exactly one each of `result`, `name`, `branch`, `sha`, and `at`. The guard
requires a fresh `GREEN` result whose branch and full source SHA match the live
PR head. It resolves the current repository before the PR identity and rechecks
both through GitHub before atomically
publishing a marker that snapshots the verification provenance. Any failed
record attempt for a valid PR removes the prior marker first.

The PR reads use `gh pr view --repo <authoritative-repository>`, while repository
identity uses `gh repo view` `nameWithOwner`; caller `GH_REPO` and
`GH_HOST` plus `MERGE_GUARD_PR_*` values are ignored and are not a supported identity source.
The active plugin version likewise
comes from the matching Claude and Codex manifests, not a caller override.

This is a breaking change in 1.0.0: callers that previously used bare
`--record-green <pr>` must first run a configured verification and pass its
result path, and callers that issued raw merges after recording must use
`merge-on-green.sh`. Existing 0.x markers must be regenerated because the 1.0.0 marker
also carries verification name, branch, SHA, timestamp, and canonical filename.

`--clear <pr>` drops a marker (e.g. after a rebase); `merge-on-green.sh` clears
the spent marker automatically after a successful merge.

`--assert-green <pr> [expected_head_branch] [snapshot_file]` is the exact
reader/enforcement
contract used by both hosts. It binds the authoritative current repository
before marker assertion, re-reads GitHub, validates every marker field and
the configured target, and exits nonzero on missing, malformed, stale, or changed
evidence. With `snapshot_file`, it returns exactly five records: authoritative
repository, head branch, head SHA, base branch, and base SHA.

The sanctioned merge acquires Git's common-directory merge lock before this
authoritative capture and holds it through post-merge verification. It performs
one final authoritative head/base read immediately before merging; any movement
blocks without invoking merge. Both that read and the merge remove caller
`GH_REPO`/`GH_HOST` values and use the identical explicit authoritative
`--repo`. It then calls `gh pr merge ... --match-head-commit H`, so a source push after the final read is rejected by
GitHub instead of merging an unverified head. GitHub exposes no equivalent
expected-base atomic primitive: target movement after the final read remains an
out-of-band branch-protection or merge-queue risk.

## Fail-closed

The classifier uses `python3`. In an initialized repository, Python or helper
absence, helper failure, malformed helper output, input decode failure, and an
unknown/Bash payload all block. There is no permissive Bash/grep parser. The hook
remains inert in a repository that has not opted in with orchestration config.

## Where it stops, and what complements it

The direct assertion governs the sanctioned merge path on every host. The hook
denies raw merge commands where supported. These are still only local layers:

- **Authenticity is bounded, not cryptographic.** The proof is a strict,
  canonical, fresh local artifact tied to a configured verification and the
  live GitHub head. A malicious process running as the same local account could
  still forge local files. Signed CI attestations or another external trust root
  are required to defend against that actor. The BL-958 owner-approved design
  gate explicitly chose this proof-binding boundary; the marker is evidence
  binding, not a signed attestation that independently proves who ran the suite.

- **The classifier is intentionally bounded.** It recognizes only a safe
  subset and does not claim to emulate the full Bash language. Classifier
  absence, unsupported syntax, ambiguous multiple merges, and malformed
  executable syntax block. It cannot prove what an arbitrary external program
  might do internally. Host-side branch protection remains necessary
  for shells or merge paths the local hook cannot govern.

- **Branch protection** (applied out-of-band via `gh api`) closes the paths the
  hook cannot see: a direct push, a merge from the GitHub UI, a non-agent shell.
  The guard and branch protection are complementary; neither alone is complete.
- **The independent review gates** decide *whether* the work is correct; the
  guard only enforces that their verdict was recorded before a merge. CI-green is
  necessary but never sufficient, which is the whole reason the marker exists
  rather than the guard just polling CI.

## Test coverage

The classifier contract and resource limits are covered directly in
[tests/merge-command-classifier.test.sh](../tests/merge-command-classifier.test.sh).
Hook/marker paths are covered in
[tests/merge-guard.test.sh](../tests/merge-guard.test.sh):
a non-merge command passes through; a commit body mentioning the words passes
through; raw merges block with or without a valid fresh marker; continuation,
dynamic-command, heredoc, redirection, and repository-redirection bypasses
block; moved-SHA, head-branch, base-SHA, plugin-version, and expired-marker
assertions block; a non-Bash tool is ignored; and the fail-closed
fallback blocks Bash payloads when the classifier is forced off. Sanctioned
exact-head race behavior is covered in
[tests/merge-on-green.test.sh](../tests/merge-on-green.test.sh).

Isolation seams for the tests: `MERGE_GUARD_STATUS_DIR` redirects the marker
directory, a controlled fake `gh` executable supplies one or two authoritative
identity reads, and `MERGE_GUARD_FORCE_FALLBACK` exercises the fallback parser
path. Tests also prove caller-provided `MERGE_GUARD_PR_*` and plugin-version
values cannot replace the authoritative GitHub and manifest responses.
