# CORSIA code repository

CORSIA owns agent history, context composition, skills, and tool routing. This
file is only a safety connector for harnesses that inspect a checkout directly;
it is not a parallel source of product or process truth.

Before changing code:

1. Follow the latest direct user scope. A later correction or narrowing wins.
2. Use the explicitly current plan and recent approved PRD + acceptance criteria
   named for that scope. If they are missing or conflict, stop before mutation.
3. Use `ARCHITECTURE.md` and current code/tests for implementation truth only.

ADRs and subsystem specs are archived historical evidence. They do not authorize
work, define current contracts, or pass a review gate.

Work on a feature branch based on `dev`. On a person's own source install,
editing that checkout directly is fine when they ask for it; `corsia restart`
serves the edits. Otherwise use a linked worktree, and never develop in or
switch a maintainer's serving checkout. Never test against the operational
database.
Use the project `sdlc-process` skill for preflight, review, and merge procedure.
