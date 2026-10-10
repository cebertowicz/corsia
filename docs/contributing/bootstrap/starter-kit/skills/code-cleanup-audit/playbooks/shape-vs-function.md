# Playbook: shape-vs-function

The differentiating track. The other seven find **obviously wrong** things; this one finds things that **look intentional but actively mislead**.

## The one question

For each structure you examine, ask: **"If I rewrote this from scratch today, with full knowledge of what it actually does, would it be shaped differently?"**

If yes, the current shape is lying. Lies in code shape cost future contributors — human or LLM — unbounded time. LLM agents are especially prone to preserving misleading shape because they pattern-match on it.

## Sub-lenses (all instances of the one question)

### 1. Over-structured for what's there

Shape suggests N cases; reality is one case with N-1 lies.

- Dispatch tables, registries, or strategy patterns with one entry.
- Prefix/suffix handlers for variants that don't occur in this codebase (speculative compatibility).
- Protocol / abstract base / interface with one implementation and no plan for a second.
- Configuration options with one documented value.
- Plugin hooks with no plugins.

**Example:** `_model_deprecates_temperature` that handles eight provider prefixes when only one is ever used. An LLM sees "this project handles Bedrock/Vertex/OpenRouter/Azure" and writes new code consistent with that — extending a fiction.

### 2. Under-structured for what's there

Shape collapses real semantic structure into a lower-fidelity type.

- `bool` where the real domain is a union (e.g., `supports_reasoning: true` when the actual semantics are effort-levels vs. token-budget vs. boolean toggle).
- `str` where the domain is an enum.
- `Any` / `unknown` where the type is knowable from context.
- Stringly-typed status fields (`"pending"`, `"done"`) where an enum would be checked at boundaries.

**Example:** `supports_reasoning: bool` in a model catalog. Looks authoritative; actually hides three different mechanisms. Frontend builds buttons for the wrong one.

### 3. Workarounds that outlived their cause

A specific bug once existed; a workaround was written; the bug was fixed; the workaround stayed and got promoted to "the way we do this."

- Sentinel values (`_X = object()`) where `None | T` expresses the same thing.
- Manual retries for errors that are now caught at a lower layer.
- Defensive null/empty checks for states the caller can no longer construct.
- Normalization code for input formats that no caller sends.
- Compat shims for clients that have all been upgraded.

**Evidence signal:** a comment near the code naming a specific bug/incident/commit that happened >6 months ago, or the workaround's git history shows it was added but never touched since the original author.

### 4. Leniency that hides truth

Global flags or patterns whose job is to make the system more permissive, which means they also hide exactly the errors a developer would want to see.

- `litellm.drop_params = True` (or equivalent per-framework flag).
- `try: ... except Exception: pass` at the top of every function.
- Default values that silently substitute when input is missing (when raising would be clearer).
- `warnings.filterwarnings("ignore", ...)` catch-alls.
- Type-coercion helpers that accept anything and return "something reasonable."

**Test:** remove the lenient flag temporarily. If tests still pass AND no real user flow breaks, the leniency was masking bugs.

### 5. Surface that points to nothing

Exposed symbols, types, or modules with no consumers.

- `__all__` entries that aren't imported anywhere else.
- `Protocol`s / `ABC`s no one subclasses.
- Public functions only called from tests.
- Re-export modules whose downstream imports all moved.
- Configuration knobs in `settings.*` read only by dead code paths.

**Distinguish from plain dead code (track 3):** dead code is code nobody runs; fossil surface is code that's reachable but whose visibility or shape suggests external consumers that don't exist.

### 6. Misleading names

Function, parameter, type, or variable names that tell a story different from what the code does. Names are sticky — readers (human or LLM) trust them, and an LLM extending the code will preserve the lie by writing more code consistent with the *name*, not the actual behavior.

- Function name promises more than the body delivers (`resolve_X` that just returns the input; `validate_X` that returns `None` regardless of input; `_send_*` that synchronously logs and never sends).
- Parameter typed as `Optional[T]` where callers always pass a value (the optionality is a fiction).
- Type / class named for what it *was* (`LegacyAdapter` that is the only adapter; `BaseClient` with a single subclass that's also the only client).
- Boolean parameter named for the unusual case (`disable_cache: bool = False` where every caller sets it to `True`; the default is the lie).
- Method named with a verb whose semantics don't match (`get_X()` that mutates; `is_Y()` that has side effects; `cleanup()` that allocates).

**Evidence signals:**
- Function body length / branching does not match the action verb in the name (e.g., a "resolver" that's a 1-line passthrough).
- Static analysis: parameter typed `Optional[T]` but every call site passes a non-None value.
- Default-versus-actual mismatch: count call sites; if every site overrides the default, the default is wrong.

> **Note:** Misleading **comments** (narrating prior edits, lying about the next statement, orphaned references) live in `playbooks/slop.md`. The comment-hygiene scope was strengthened and consolidated there. The review step deduplicates findings that span both tracks.

## Heuristics for sharpening shape-vs-function findings

When a candidate shape-mismatch is borderline, apply these to decide:

- **Consumer count.** If zero external consumers → probably fossil. If one → probably speculative abstraction. If three+ → probably real.
- **Git age + last-touched.** Old + untouched → more likely fossil. Recent changes → more likely load-bearing.
- **Commit message linkage.** Was the shape introduced to fix a specific bug that's now closed? If the originating bug is fixed and the shape outlives it, report.
- **Would removing it simplify or complicate?** If removal means writing *more* code elsewhere to replicate what the shape does, it's probably real. If removal just deletes lines, it's probably fossil.
- **Would an LLM contributor extend the pattern?** If yes, and extending it would propagate the lie, it's a high-priority finding.

## Output

Findings from this track are almost always `MEDIUM` confidence. Shape-vs-function is inherently judgment-heavy — report, don't implement, unless:

- The fossil has zero consumers AND no git activity for 6+ months (HIGH: suggest deletion).
- The sentinel has exactly one call site (HIGH: suggest replacement with `None`).

Everything else: report as MEDIUM with the suggested fix direction only. The human (or a subsequent PR) decides.

## What NOT to flag

Avoid the aesthetic-complaint failure mode. Do not flag:

- Code that's simply verbose but honest about what it does.
- Duplicate code that's intentionally duplicated for independence (e.g., two separate validators with identical bodies but different domain meanings).
- Defensive code at real system boundaries (untrusted input, external APIs, user-supplied data).
- Thin wrappers that exist to encapsulate testing or observability.

The line: "shape overstates function" is a finding. "Could be written more concisely" is not.
