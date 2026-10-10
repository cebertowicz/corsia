# Playbook: cycles

Find circular imports — modules that import each other, directly or transitively. Cycles hurt startup-time reasoning, testability (can't import one without the other), and layering clarity.

## What to look for

- Direct cycles: `A imports B; B imports A`.
- Indirect cycles: `A → B → C → A`.
- Deferred-import workarounds (`import X` inside a function) that reveal a latent cycle.
- Late imports inside TYPE_CHECKING blocks that mask a real structural cycle.

## Evidence to collect

- Full import graph of the target paths (dict of module → list of imports).
- Cycle detection: walk the graph, report each strongly-connected component with >1 node.
- For each cycle: the files, the import lines, and whether any of them are inside functions (deferred).
- Git history: did the cycle exist from the start, or was it introduced incrementally?

## Rules

- **Don't just move imports around.** Fixing a cycle requires identifying the right layer boundary. Report the cycle + suggested direction; human decides.
- **Deferred imports are a smell AND a fix.** If `A → B → A` is avoided by a deferred import inside a function, the code runs — but the cycle is real and misleading. Flag as MEDIUM.
- **Circular imports within a package are less bad than across packages.** A cycle inside `backend/tasks/*` is cohesive; a cycle between `backend/tasks/` and `backend/auth/` suggests a bad dependency direction.

## Confidence tiers

- `HIGH`: Direct two-file cycle, both imports at module level, both imports actually needed at module level. Report with suggested refactor direction (usually: extract a shared third module for what they both need). Do NOT implement — the fix is a design call.
- `MEDIUM`: Cycle avoided by deferred imports. Report the structural issue.
- `LOW`: Transitive cycle through 4+ modules. Report only if it crosses a major layer boundary.

## What NOT to flag

- Mutual references inside a single module (classes referencing each other in the same file).
- Cycles that exist only in test files.
- Cycles through a "shared types" module that everyone reasonably needs.

## Output

Report each cycle with:
- The modules in the cycle.
- The specific import lines (file:line).
- Whether any are deferred.
- Suggested break direction (which module should not need the other).
