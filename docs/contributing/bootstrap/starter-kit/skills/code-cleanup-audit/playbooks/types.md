# Playbook: types

Find shared types that have drifted — the same concept represented by different types in different places, or duplicate type definitions that used to agree and no longer do.

## What to look for

- `User`, `Session`, `Message` (or similar domain-core types) defined in multiple files with slightly different field sets.
- TypedDicts / Pydantic models / TypeScript interfaces with the same *name* but different shape in different modules.
- Stringly-typed data where the canonical type is defined somewhere but not imported.
- Type aliases for the same concept with different names (`UserId = str`, `UserID = str`, `user_id: str`).
- Migration from one type system to another (Pydantic v1 → v2, interface → type) where both still exist.

## Evidence to collect

- Definitions sorted by file path + name collisions.
- Field-by-field diff where shapes diverge.
- Import graph: who uses which definition.
- Which one is the "canonical" or most-used version.

## Rules

- **Converge on one definition per concept.** Pick the most-used one as canonical; migrate others with re-exports for backward compat.
- **Never silently widen a type.** If type A has field `x: int` and type B has `x: int | None`, merging them to `int | None` may break callers of A. Flag as MEDIUM.
- **Frontend and backend drift is a common case.** The server sends `User { id, name }`; the frontend defines `User { id, name, avatar_url }`. Flag both and suggest the canonical source.

## Confidence tiers

- `HIGH`: Two definitions with identical fields in different locations, one with more imports. Merge to the canonical version.
- `MEDIUM`: Definitions that mostly agree but have diverged on one or two fields. Report the diff.
- `LOW`: Semantically related but intentionally distinct types (e.g., `InputUser` and `OutputUser` in an API layer). Report only if naming suggests confusion.

## What NOT to flag

- DTOs intentionally shaped differently at API boundaries.
- Test fixtures / factories that construct simplified versions of a domain type.

## Output

Report each divergence with:
- The N definitions and their files.
- Field-by-field diff.
- Import-count for each.
- Suggested canonical definition.
