# UI-E2E flows

> Real browser coverage drives the real frontend against the real backend.
> This is the layer that exercises user-visible flows, layout, focus, mobile
> breakpoints, and accessibility regressions.

Specs live in `frontend/e2e/`. Canonical user flows live in
`frontend/e2e/flows/`. Smoke + visual baselines live at the e2e/ root
(`corsia-smoke.spec.ts`, `corsia-visual.spec.ts`).

CI workflow: `.github/workflows/e2e.yml` — runs on PRs + pushes to `main`.

---

## TL;DR — how to run

Requires `CORSIA_E2E_TOKEN` set to an API token and
`CORSIA_E2E_SESSION_TOKEN` set to a browser-session token from one disposable
seeded test account. `scripts/e2e-local` provisions both; direct Playwright
runs must provide both. There are no built-in credentials.

```bash
cd frontend
export CORSIA_E2E_TOKEN=...       # API token from the disposable test account
export CORSIA_E2E_SESSION_TOKEN='<disposable browser-session token>'
npm ci
npx playwright install --with-deps chromium
npm run e2e                       # interaction flows + smoke
npm run e2e:visual                # visual regression baselines (opt-in)
npx playwright test flows/        # only the slice-3 canonical flows
npx playwright test --headed      # watch in a real browser
npx playwright test --ui          # Playwright UI mode (best for debug)
```

Test artifacts (HTML report + traces + videos on failure):
- `frontend/playwright-report/`
- `frontend/test-results/`

---

## Add a new flow in <30 minutes

1. **Decide the flow's name.** Use kebab-case mirroring what the user
   does (e.g. `cost-dashboard-loads.spec.ts`, not `test4.spec.ts`).
2. **Copy `flows/chat-send-receive.spec.ts`** as the template — it walks
   login → action → assertion in the canonical shape. Most flows fit
   the same skeleton.
3. **Pick selectors.** Order of preference (top = best):
   - **ARIA role + accessible name:** `page.getByRole('button', { name: 'Send' })`
   - **Visible text:** `page.getByText('No conversations yet')`
   - **Placeholder:** `page.getByPlaceholder('Message CORSIA...')`
   - **CSS class hook:** `page.locator('.message.assistant')` — last resort
   - **`data-testid`:** preferred for new selectors when no accessible
     name exists; the codebase uses none today, but new components
     should prefer it.
4. **Wait explicitly.** `await expect(...).toBeVisible({ timeout })`.
   **NEVER** `page.waitForTimeout` (one exception: race-window setup
   for cancel-and-replace in `flows/mid-turn-cancel-and-replace.spec.ts`,
   documented in-line).
5. **Run locally.** `npx playwright test flows/your-spec.spec.ts --headed`.
6. **Run 5x for flake calibration.** `for i in 1 2 3 4 5; do npx
   playwright test flows/your-spec.spec.ts; done`. Target: 5/5 green.
   If you see ≥1 flake, root-cause before merging — see "Common pitfalls"
   below.
7. **Tag the spec.** Top of `test.describe`: `@flow:<name>
   @surface:<area>`. The tags drive test selection in CI matrices and
   the `qa.config.json` impact map.
8. **Open the PR.** CI runs the suite; the workflow uploads the HTML
   report on failure for triage.

---

## Worked example — `chat-send-receive.spec.ts`

The simplest canonical flow. Walk through:

```ts
test('typed message sends, assistant responds, both render in order', async ({ page }, testInfo) => {
  await loginByUrl(page);                                  // ← seeded auth helper
  await expect(page.getByRole('heading', { level: 2 })).toBeVisible();   // ← empty-state sanity

  const prompt = 'Reply with the single word OK and nothing else.';
  await sendChatMessage(page, prompt);                     // ← composer + Enter

  await expect(lastUserMessage(page)).toContainText(prompt, { timeout: 10_000 });
  await expect(lastAssistantMessage(page)).toBeVisible({ timeout: 60_000 });
  await expect(lastAssistantMessage(page)).not.toBeEmpty({ timeout: 60_000 });

  await expect(chatComposer(page)).toHaveText('', { timeout: 5_000 });   // ← composer clears
});
```

What each piece does:

- **`loginByUrl(page)`** (in `support/corsia.ts`) — signs in via the
  `?token=` query param using `CORSIA_E2E_TOKEN` (required, no default).
  Avoids driving the auth-gate UI when auth itself isn't what you're
  testing. Cuts ~2s per spec.
- **`sendChatMessage(page, prompt)`** — types into the contenteditable
  composer + presses Enter. Mirrors a real human; goes through the same
  `handleKeydown` handler the production UI walks. Doesn't call the API
  directly so the spec exercises the full client-stack.
- **`expect(lastUserMessage(page)).toContainText(prompt, { timeout: 10_000 })`** —
  asserts the optimistic-render path: user bubble appears before the
  WS round-trip. 10s is the default `expect.timeout` from
  `playwright.config.ts`; the explicit value is for readability, not
  necessity.
- **`expect(lastAssistantMessage(page)).toBeVisible({ timeout: 60_000 })`** —
  60s ceiling for the LLM round-trip. Real LLM turns take 1-15s; the
  60s ceiling is for the worst case (cold provider, slow network). If
  a turn takes >60s we have a real regression, not a flake.

---

## Common pitfalls (and how the slice surfaced them)

### 1. Implicit waits are flake bait.

`page.click('.send-btn'); page.locator('.message.assistant').textContent()`
will pass when the network is fast and fail when it's slow. Always
guard with `await expect(...).toBeVisible({ timeout })` — Playwright's
auto-wait only triggers on actions, not assertions.

### 2. `page.waitForTimeout(...)` hides race conditions.

Used legitimately ONCE in this codebase, in the cancel-and-replace
flow's mid-flight delay. That's a deliberate race-window setup, not a
flake suppressor. Anywhere else, replace with an explicit
`expect(...).toBeVisible()`.

### 3. Mobile vs desktop selectors.

The chat composer is the same `role="textbox"` element across both
viewports — selectors don't need viewport-specific branches there. But
the sidebar IS hidden behind a "Toggle sidebar" button on mobile; if
your flow asserts something inside the sidebar, open it first:

```ts
if ((page.viewportSize()?.width ?? 0) <= 768) {
  await page.getByRole('button', { name: 'Toggle sidebar' }).click();
}
```

### 4. Console errors are signal, not noise.

Flow 5 (`settings-panel-opens.spec.ts`) uses
`captureConsoleErrors(page)` and asserts `expect(errors).toEqual([])`.
If your flow exercises a complex surface, copy that pattern. The first
console error is usually a real bug — root-cause it; don't allow-list
it.

### 5. Backend startup timing.

`playwright.config.ts` declares the backend + frontend as `webServer`s
with a 120s startup timeout. If you see "Timed out waiting for backend
on port X" in CI but not locally, check whether your changes added
import-time work to the backend (model loaders, provider probes). Push
that work into a lazy path; the smoke `/api/health` route should
respond in <5s on a cold boot.

### 6. Real LLM provider keys in CI.

Flows 1 and 3 require a real LLM round-trip. CI must inject
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY` (or both) as repo secrets;
without them the providers won't resolve and the spec hangs until the
60s ceiling. The workflow at `.github/workflows/e2e.yml` plumbs both.

---

## Flow inventory

| File                                                         | Surface         | Live LLM? | Wave 2 gate |
|--------------------------------------------------------------|-----------------|-----------|-------------|
| `flows/chat-send-receive.spec.ts`                            | chat            | yes       | no          |
| `flows/mid-turn-cancel-and-replace.spec.ts`                  | chat            | yes       | no          |
| `flows/history-persists-across-reload.spec.ts`               | chat / history  | yes       | **yes**     |
| `flows/sidebar-renders.spec.ts`                              | navigation      | no        | no          |
| `flows/settings-panel-opens.spec.ts`                         | settings        | no        | no          |

Wave-2-gated flows skip when `CORSIA_E2E_TRIM_FIX` is unset or `0`. CI
flips the flag once Slice 5's `_trim_to_window` fix lands on `main`.

---

## Where this layer fits

- **Test layering** — unit (pytest), integration-via-harness (Inspect AI),
  and UI-E2E (Playwright + agent-browser). This directory owns UI-E2E.
- **`qa/`** — agent-browser scaffolding for exploratory probes (UX
  reviews, surface discovery). Not in scope for the canonical flows
  here; canonical flows are deterministic Playwright specs.
- **Visual regression** — `corsia-visual.spec.ts` + the
  `corsia-visual.spec.ts-snapshots/` folder. Opt-in via
  `CORSIA_VISUAL=1`. Pixel baselines are platform-pinned to darwin;
  the linux CI runner does not produce matching pixels and the
  `@visual` project skips by default in `npm run e2e`.

---

## When in doubt

Ask: **"At what layer does the bug manifest fastest?"**

- Pure-function bug → unit test (pytest).
- Multi-component / state-machine / context-builder bug → integration
  scenario (YAML in `~/.corsia/test-scenarios/`).
- Layout / focus / mobile / accessibility / "the panel doesn't render"
  bug → here.

Don't write a UI-E2E test for a parser bug; don't write a unit test for
a focus-trap bug. Earn refactor-on-review by mismatching the layer.
