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

Work on a feature branch in a linked worktree based on `dev`. Never develop in
or switch the serving checkout, and never test against the operational
database.
Use the project `sdlc-process` skill for preflight, review, and merge procedure.
