---
name: recover-agent-work
description: Recover the uncommitted work of a background or subagent that died, was rate-limited, crashed, timed out, or was interrupted mid-task, instead of re-running it from scratch. Use when an implementer agent stopped without opening its PR, when you see a rate-limit or crash on a spawned agent, or when a worktree has changes but no commit. The work is almost always still on disk in the agent's worktree.
---

# Recover a dead agent's work

When a background agent dies (rate limit, crash, timeout, interruption), its work
is not lost. It is sitting UNCOMMITTED in its git worktree. The `Stop`-hook sweep
that cleans up finished worktrees deliberately skips any worktree with a dirty
tree, precisely so a dead-mid-edit agent's changes survive for recovery. Do not
re-run the ticket from scratch; recover what is already there.

## The key facts

- A finished, clean agent worktree is swept. A dirty one is preserved. So if the
  agent got far enough to write files, they are still on disk.
- Branch refs live in the shared `.git`. If the agent committed but did not push,
  the commit is safe even if the worktree were removed.
- The work must be committed **from inside the worktree path**, not from the main
  repo checkout. Committing from the wrong working directory is how recovered
  work gets stranded on the wrong branch or lost again.

## Procedure

1. **Find the worktree.** List them and locate the agent's:
   ```
   git worktree list
   ```
   Agent worktrees live under the configured `worktree_base` (default
   `.claude/worktrees/`) with an `agent-*` name. Match it to the ticket.

2. **See what survived.** Inspect the worktree's tree without changing directory
   into the main repo:
   ```
   git -C <worktree> status
   git -C <worktree> diff
   ```
   Uncommitted changes here are the recoverable work.

3. **Read the agent's intent.** If a transcript or task output for the dead agent
   is available, read it for how far it got and what its own verdict was (tests
   passing, PR intended, blockers hit). Recover to the state it was aiming for,
   not a guess.

4. **Commit from the worktree.** Run the commit with the worktree as the working
   directory, so it lands on the agent's branch:
   ```
   git -C <worktree> add -A
   git -C <worktree> commit -m "<conventional message referencing the ticket>"
   ```
   Before committing, make the work actually mergeable: run the repo's
   `self_check` commands from config in the worktree, and finish anything the
   agent left half-done. A recovered commit still has to pass the same gates.

5. **Open the PR yourself.** Push the branch and open the PR to the integration
   branch, exactly as the implementer would have:
   ```
   git -C <worktree> push -u origin HEAD
   gh pr create --base <integration_branch> ...
   ```
   Then it re-enters the normal pipeline at the gate stage.

## Do not

- Do not `cd` into the main repo and commit there; the changes are in the
  worktree, on the agent's branch.
- Do not delete or force-remove the worktree until the work is committed and
  pushed. `git worktree remove --force` on a dirty worktree discards exactly the
  work you are trying to save.
- Do not restart the ticket from an empty branch when a dirty worktree exists;
  that duplicates effort and risks two branches for one ticket.
