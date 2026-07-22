---
name: scope-ticket
description: Turn a thin or vague ticket into one you can write a failing test from, before any branch is cut. Use when about to implement a ticket whose description is underspecified, when asked to scope/refine/groom a ticket, or when a task lacks clear acceptance criteria or a defined entry point. The bar is a single question - could you write a red test from this cold? If not, it is not ready to implement.
---

# Scope a ticket to "Ready"

A ticket is Ready when someone with no prior context could write a failing test
from it and know exactly when it is done. Most defects that reach production
trace back to a ticket that was implemented while still ambiguous: the agent
guessed, and the guess shipped. Scoping first is cheaper than re-work.

Do not cut a branch on a ticket that is not Ready. Scope it, or push it back.

## Fill every section

Work the ticket into these five sections. If you cannot fill one, that gap is
the thing to resolve before implementing.

1. **Behavior.** What the system should do, in the user's terms, not the
   implementation's. One or two sentences. If you cannot state it without naming
   internal functions, the requirement is not understood yet.

2. **Acceptance Criteria.** Specific, testable statements. Each one must map to a
   test you could write now. "Works correctly" is not a criterion; "an anonymous
   visitor sees the price from the database, not the template default" is.
   Include the values, labels, and states that matter.

3. **Entry Points.** Where a real user reaches this, in concrete clicks or
   routes. This is the guard against the most common silent failure: shipping a
   column, a function, a component, or a CSS class that nothing wires to a
   user-visible surface. If the entry point is undefined, the feature is not
   scoped, it is half-imagined.

4. **Edge Cases.** Empty states, error states, boundaries, permissions, the
   unauthenticated path, the too-many and the zero cases. Name the ones that
   apply; note the ones you are deliberately not handling.

5. **Out of Scope.** What this ticket explicitly does NOT do. This is what keeps
   the implementation from sprawling and what protects the next ticket's turf.

## The readiness test

After filling the sections, ask the one question that decides it:

> Could a fresh implementer write a failing test from this, today, without
> asking a clarifying question?

- **Yes** -> Ready. It can be pulled into implementation.
- **No** -> Not Ready. The specific missing piece (an unstated value, an
  undefined entry point, an ambiguous behavior) is the next thing to resolve.
  Resolve it or send the ticket back; do not paper over it with a half-feature.

## Output

Produce the ticket rewritten into the five sections, then state the verdict
(Ready / Not Ready) and, if Not Ready, the exact gap that blocks it. If the
repo's `rules_docs` define a ticket template or extra required fields (a
surface-area label, a definition-of-done clause), honor that template too.
