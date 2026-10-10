# Playbook: strong-types

Find places where a weaker type is used than the code actually allows. `Any`, `dict`, `object`, untyped parameters, stringly-typed enums — all places where a real type could replace them with zero runtime change.

## What to look for

- `Any` / `unknown` / `*args` / `**kwargs` where the call site uses a known-shape dict.
- `dict` / `dict[str, Any]` where a TypedDict or dataclass would be more honest.
- Stringly-typed enums (`status: str` with comment "one of: pending|done|failed").
- `Optional[T]` where callers always pass a value.
- Type hints on signatures but no type hints on internal variables that would document the shape.
- Deserialized JSON handled as raw dicts instead of parsed into a typed model.

## Evidence to collect

- For each weak type: the call sites and the values actually passed.
- Whether the function's body uses specific fields of the argument (if yes, a dict or TypedDict reveals those fields).
- Language-specific: check for `# type: ignore` or `any as` TS escapes — they often mark spots where the type wasn't worked out.

## Rules

- **Strengthening a type must not change runtime behavior.** A `dict` → TypedDict migration is invisible to Python at runtime. An `Optional[str]` → `str` migration that adds a runtime check is behavior-changing; report, don't implement.
- **Don't over-type.** `Any` is sometimes correct — generic serializers, pass-through wrappers. Only flag where the actual type is clearly knowable.
- **TypedDict > dataclass > pydantic** for simple cases where no runtime validation is needed. Don't migrate to a heavier tool for its own sake.

## Confidence tiers

- `HIGH`: `Any` in a signature, single caller, caller passes a literal dict with fixed keys. Suggest TypedDict definition.
- `MEDIUM`: `dict` used broadly, many callers with varying shapes. Report but don't attempt a unifying type — might need a discriminated union that's a design call.
- `LOW`: Stringly-typed status in hot paths. Report with suggested Literal / Enum.

## What NOT to flag

- Generic utility functions that are legitimately polymorphic.
- Boundary parsers that accept raw untyped data from external sources.
- Test helpers and mocks that need flexible shapes.

## Output

Report each weak-type finding with:
- File:line of the weak type.
- What type it should be, with evidence.
- Whether strengthening is runtime-safe (HIGH) or requires a design call (MEDIUM).
- A sketch of the stronger type (without writing the full migration).
