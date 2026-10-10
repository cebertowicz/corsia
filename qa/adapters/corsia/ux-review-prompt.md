# CORSIA Local UX Review Prompt

Use this prompt with a local multimodal model after deterministic tests produce screenshots, traces, and a changed-file summary. The reviewer is advisory until deterministic tests pass.

## Inputs

- Surface id and expected behavior from `qa/adapters/corsia/surfaces.yml`.
- Changed files and selected impact rules from `node qa/core/cli.mjs select --json`.
- Before/after screenshots or Playwright visual diff output.
- Relevant design rules from `frontend/DESIGN-SYSTEM.md`.
- Console errors, failed network requests, and accessibility snapshot when available.

## Task

Review the UI as a senior product-minded QA engineer. Decide whether the visual or interaction change is acceptable, risky, or bad UX.

## Rubric

- Intended delta: Does the changed UI match the purpose of the code change?
- Regression risk: Did unrelated surfaces move, resize, disappear, overlap, clip, flicker, or lose affordance?
- Taste: Is the result visually coherent with CORSIA's existing dense, work-focused design system?
- Ergonomics: Are primary workflows still obvious, fast, keyboard/touch friendly, and reversible?
- Accessibility: Are controls named, focusable, readable, and usable at desktop and mobile sizes?
- Failure behavior: Are loading, empty, offline, and error states explicit rather than silent?

## Output

Return JSON:

```json
{
  "status": "pass | warn | fail",
  "surface": "surface.id",
  "summary": "One concise sentence.",
  "findings": [
    {
      "severity": "blocker | high | medium | low",
      "kind": "regression | intended-delta | ux-taste | accessibility | flake-risk | review-runtime",
      "evidence": "Screenshot/trace/test artifact reference.",
      "recommendation": "Concrete change or question."
    }
  ],
  "questions": []
}
```

Do not fail purely because pixels changed. Fail when the change is unintended, incoherent, inaccessible, or likely to confuse users.
