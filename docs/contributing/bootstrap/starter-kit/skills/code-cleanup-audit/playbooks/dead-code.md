# Playbook: dead-code

Find functions, classes, modules, and exports with zero consumers. The trickiest track — a "dead" function may be consumed by a plugin, a CLI entry point, a test runner, or an external downstream package. **Manual verification is required for every finding before implementation.**

## What to look for

- Functions defined but never called.
- Classes instantiated only in their defining module.
- Exports in `__all__` / `index.ts` not imported elsewhere.
- Entire files that no other file imports (orphaned modules).
- Helper functions that exist only because a larger call site was removed.
- Commented-out code blocks >5 lines old.

## Evidence to collect

For each candidate:
- Full-repo grep for the symbol name.
- Check `__all__` / `index.ts` / any other re-export surface.
- Check entry points in `pyproject.toml`, `package.json` scripts, `Makefile`, CLI definitions.
- Check test files separately (used-only-in-tests is still suspicious but lower priority to remove).
- Git log: when was the function last modified? If it was added recently and hasn't been called, it might be WIP.

## Rules

- **Grep is necessary but not sufficient.** Dynamic dispatch (`getattr`, string-based calls, plugin registries) can mask real consumers. If the function's name appears in any string literal, err on the side of keeping it.
- **Public interfaces of a library need stronger evidence.** If the project is a package (has a `pyproject.toml` or `package.json` for publishing), removing an export is a breaking change even if no internal code calls it.
- **Test-only consumers are a signal, not proof.** If a function is only called from tests, someone may have deleted its production usage and forgotten the function itself. But it's also possible the function was always internal and the test is load-bearing. Flag as MEDIUM either way.
- **Entry points count.** CLI commands, FastAPI route handlers, background task functions, migration scripts — all have consumers that don't show up in grep.

## Confidence tiers

- `HIGH`: Zero grep matches anywhere, zero entry-point matches, zero dynamic-dispatch string matches, not in `__all__`. Remove.
- `MEDIUM`: Has call sites but all in removed/commented code, or only in tests. Report for review.
- `LOW`: Name might collide with other things (common names like `process`, `handle`) — grep is unreliable. Report only if other signals suggest it's dead.

## What NOT to flag

- Abstract/protocol methods that are overridden.
- Entry points registered in external configuration.
- Fixtures and factories in conftest.py.
- Functions exported for external library consumers (check if this is a published package).

## Output

Report each dead-code finding with:
- The symbol and its file:line.
- All places it *could* be called from (grep output).
- What checks you ran (full-repo grep, __all__ scan, entry points scan, etc.).
- Confidence tier and reasoning.
- If HIGH: a suggested removal diff.
