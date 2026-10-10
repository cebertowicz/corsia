# Playbook: errors

Find silent failures, swallowed exceptions, misleading defaults, and error-handling that hides truth. This is close cousin to the shape-vs-function track's "leniency that hides truth" category — handle both here for consistency; the review step dedupes.

## What to look for

- `try: ... except: pass` (catch-all, no logging).
- `except Exception as e: log.debug(e); return None` — logs but at a level that's effectively invisible.
- Default return values that swallow real errors (`return "" if failed` where the caller expected a value).
- Retry loops that retry errors that shouldn't be retried (permission denied, 400 Bad Request).
- Retry loops with no max attempts.
- `warnings.filterwarnings("ignore")` scoped broader than the specific warning.
- Global exception hooks that silence everything.

## Evidence to collect

- For each silent-catch: what exceptions can actually fire inside the try block?
- For each default-swallow: do any callers distinguish "the real value is empty" from "the operation failed"?
- For retries: what error classes do they retry? Is there a deterministic-error in there that shouldn't retry?

## Rules

- **Silencing is sometimes correct.** A log-and-continue for a non-critical background task is fine. Flag patterns where the silenced error could matter.
- **Don't "fix" by raising.** Changing a silent catch to a raise is behavior-changing. Report the issue; suggest the direction; human decides.
- **Logs at DEBUG are effectively silent.** If an error is caught and logged at DEBUG only, it's no better than `pass` in production.

## Confidence tiers

- `HIGH`: Literal `except: pass` with no logging, in a non-test file. Report with suggested narrowing of the exception class.
- `MEDIUM`: Broad `except Exception`, logged but at a level that won't be seen. Report.
- `LOW`: Retry logic that might retry the wrong things. Report only if the caller surface would care.

## What NOT to flag

- Test code with broad except blocks.
- Cleanup paths (`finally`) that catch errors to ensure unlocking.
- Defensive error handling at process boundaries (e.g., a webhook handler that can't let one bad request crash the whole server).

## Output

Report each swallowed-error finding with:
- File:line of the except clause.
- What exceptions can fire inside the try.
- The silencing pattern (pass, debug-log, default-return).
- Suggested narrowing: which specific exceptions *should* be caught, and what the non-caught ones should do instead.
