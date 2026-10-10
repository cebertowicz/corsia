import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import path from "node:path";
import {
  createSession,
  loadPastFirstRunLanding,
  openAppByUrl,
  saveEvidenceScreenshot,
} from "./support/corsia";

/**
 * PLANS-AC34 + the composed host contract, in a real browser.
 *
 * Two halves, deliberately:
 *  - the `/_design` showcase drives every dock state through the production
 *    component and store;
 *  - the real shell proves the dock is reachable where the user actually is,
 *    with only `/api/attention/queue` intercepted.
 */

const screenshotDir = process.env.PLANS_SCREENSHOT_DIR;

const SOURCE = {
  thread_id: "child",
  thread_label: "Reviewer subagent",
  project_id: "project-1",
  project_label: "CORSIA",
  archived: false,
  parent_reviewed: true,
};

function decision(over: Record<string, unknown> = {}) {
  return {
    id: "decision-1",
    plan_id: "plan-1",
    node_id: "node-decision",
    source_node_id: "node-source",
    thread_id: "child",
    title: "Where should decision state live?",
    context: "P5 needs one canonical home for decision content.",
    tradeoff: "A table is typed; the identity avoids a second row.",
    recommendation: "B",
    rationale: "CORSIA owns the identity, so no twin is required.",
    options: [
      { id: "A", label: "Ship the twin", detail: "Adds one table." },
      { id: "B", label: "Reuse the identity", detail: "No schema change." },
    ],
    other_enabled: true,
    order_key: 0,
    revision: 3,
    raised_at: "2026-08-02T10:00:00+00:00",
    source: SOURCE,
    ...over,
  };
}

/** Intercepts only the queue; every other route stays the real backend. */
async function seedQueue(page: Page, current: unknown, count: number) {
  await page.route("**/api/attention/queue", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      // The backend supplies the list; the store reads `items`, not `current` alone.
      body: JSON.stringify({ current, count, items: current ? [current] : [] }),
    });
  });
}

async function shot(page: Page, name: string) {
  if (!screenshotDir) return;
  await page.screenshot({
    path: path.join(screenshotDir, `${name}.png`),
    fullPage: true,
  });
}

async function selectThread(page: Page, title: string) {
  if ((page.viewportSize()?.width ?? 0) <= 768) {
    await page.getByRole("button", { name: "Toggle sidebar" }).click();
  }
  // The title also matches non-row text once Activity lists it; select the sidebar row.
  await page.locator(".thread-item").filter({ hasText: title }).click();
  // The composer is also visible on the thread being left, so it cannot show
  // the selection landed; the row turning active is the app's own marker, and
  // the attention stack re-renders from the same state.
  await expect(
    page.locator(".thread-item.active").filter({ hasText: title }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("textbox", { name: "Message CORSIA" }),
  ).toBeVisible({ timeout: 20_000 });
}

async function openThread(
  page: Page,
  request: APIRequestContext,
  label: string,
) {
  const title = `${label}-${Date.now()}`;
  await createSession(request, title);
  // A fresh workspace has a first-run landing: once /api/conductor resolves with
  // first_run_pending, the app opens the conductor thread and loads its
  // messages. Selecting a row before that lands races it, and the conductor can
  // end up on screen with its model-sources panel in the attention stack.
  await loadPastFirstRunLanding(page, () => openAppByUrl(page), { lands: true });
  await selectThread(page, title);
  return title;
}

test.describe("@surface:attention The one attention dock — /_design (PLANS-AC34)", () => {
  test("every dock state renders through the production component", async ({
    page,
  }, testInfo) => {
    await page.goto("/_design#attention-dock");
    await expect(
      page.getByRole("heading", {
        name: "Attention dock — the one thing that needs you",
      }),
    ).toBeVisible();

    // The decision drawer is portaled to document.body, outside #attention-dock.
    const showcase = page;

    // 1 waiting — a single quiet line, no body until asked.
    await showcase
      .getByRole("button", { name: "1 waiting", exact: true })
      .click();
    const bar = showcase.locator(".dock-bar").first();
    await expect(bar).toBeVisible();
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await expect(
      showcase.getByText("Completed branches should collapse"),
    ).toBeHidden();

    // Explicit open — the decision moves into the reusable drawer and takes focus.
    await bar.click();
    const drawer = showcase
      .getByRole("dialog", {
        name: "Decision — How much completed work should the landing canvas retain?",
      })
      .first();
    await expect(drawer).toBeVisible();
    await expect(showcase.locator(".decision-chip").first()).toBeVisible();
    await expect(
      showcase.getByText("Completed branches should collapse").first(),
    ).toBeVisible();
    await expect(
      drawer.getByRole("button", { name: "Answer", exact: true }),
    ).toBeInViewport();
    await shot(page, "dock-01-desktop-open");
    await saveEvidenceScreenshot(page, testInfo, "dock-desktop-open");

    // Other → listed option: the head defect. The draft clears and Answer arms.
    const other = showcase.locator(".other-option textarea").first();
    await other.fill("Actually, split this into two.");
    await expect(showcase.locator(".other-option").first()).toHaveClass(
      /selected/,
    );
    await showcase
      .getByRole("button", { name: /Seen on canvas open/ })
      .first()
      .click();
    await expect(other).toHaveValue("");
    await expect(
      showcase.getByRole("button", { name: "Answer", exact: true }).first(),
    ).toBeEnabled();
    await shot(page, "dock-02-other-then-option-recovered");
    // The drawer's close control is its trailing "Done" (AdaptiveDrawer doneLabel).
    await drawer.getByRole("button", { name: "Done", exact: true }).click();
    await expect(showcase.locator(".decision-chip")).toHaveCount(0);

    // 0 waiting — the dock renders nothing at all.
    await showcase.getByRole("button", { name: "0 waiting" }).click();
    await expect(showcase.locator("[data-attention-dock]")).toHaveCount(0);
    await shot(page, "dock-03-empty-calm");

    // Many waiting — head only, plus a count.
    await showcase.getByRole("button", { name: "Many waiting" }).click();
    await expect(showcase.getByText("4 waiting").first()).toBeVisible();
    await expect(showcase.locator(".decision-chip")).toHaveCount(0);

    // Root-direct provenance is labelled, not implied.
    await showcase.getByRole("button", { name: "No parent review" }).click();
    await showcase.locator(".dock-bar").first().click();
    await expect(
      showcase.getByText("Raised directly — no parent review").first(),
    ).toBeVisible();
    await shot(page, "dock-04-root-direct-labelled");
    await showcase.getByRole("button", { name: "Done", exact: true }).click();

    // External review hands off and never resolves someone else's object.
    await showcase.getByRole("button", { name: "External review" }).click();
    await showcase.locator(".dock-bar").first().click();
    await expect(
      showcase.getByRole("button", { name: /Open in documents/ }).first(),
    ).toBeVisible();
    // Scope to the open drawer: the page also holds the showcase's own controls.
    const handoff = showcase.getByRole("dialog");
    await expect(handoff.getByRole("button", { name: /^Clear/ })).toHaveCount(0);
    await expect(handoff.getByRole("button", { name: /^Dismiss/ })).toHaveCount(
      0,
    );
    await shot(page, "dock-05-external-review-handoff");

    // A host lease awaiting the user takes the slot; the dock defers.
    await showcase
      .getByRole("button", { name: "Host lease holds the slot" })
      .click();
    const suppressed = showcase.locator(".dock-bar").first();
    // Deferred, not removed from the tab order — and it says why.
    await expect(suppressed).toHaveAttribute("aria-disabled", "true");
    expect(
      await suppressed.evaluate((element) => (element as HTMLButtonElement).disabled),
    ).toBe(false);
    await expect(
      showcase.getByText(/after the host-access request/i),
    ).toBeVisible();
    await suppressed.click({ force: true });
    await expect(showcase.locator(".decision-chip")).toHaveCount(0);
    await shot(page, "dock-06-lease-suppression");

    // Stale keeps last-known data and says so.
    await showcase.getByRole("button", { name: "Stale / offline" }).click();
    await expect(
      showcase.getByText("Showing last known items.").first(),
    ).toBeVisible();
    await shot(page, "dock-07-stale");
  });

  test("keyboard-only operation and reduced motion hold at mobile widths", async ({
    page,
  }) => {
    // The outer showcase must be desktop-width in both projects; only the iframes are narrow.
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/_design#attention-dock");
    // The decision drawer is portaled to document.body, outside #attention-dock.
    const showcase = page;
    await showcase
      .getByRole("button", { name: "1 waiting", exact: true })
      .click();

    const bar = showcase.locator(".dock-bar").first();
    await bar.focus();
    await expect(bar).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(showcase.locator(".decision-chip").first()).toBeVisible();

    // Focus lands on the chip heading, never on <body>.
    await expect(showcase.locator(".decision-chip h3").first()).toBeFocused();

    const option = showcase
      .getByRole("button", { name: /Seen on canvas open/ })
      .first();
    await option.focus();
    await page.keyboard.press("Enter");
    await expect(option).toHaveAttribute("aria-pressed", "true");

    // Escape collapses and returns focus to the bar it came from.
    await page.keyboard.press("Escape");
    await expect(showcase.locator(".decision-chip")).toHaveCount(0);
    await expect(showcase.locator(".dock-bar").first()).toBeFocused();

    for (const [label, width, height, name] of [
      ["390", 390, 844, "dock-08-mobile-390"],
      ["320", 320, 720, "dock-09-mobile-320"],
    ] as const) {
      await showcase.getByRole("button", { name: label, exact: true }).click();

      // A container width cannot fire a viewport media query, so the mobile
      // modes are real iframes with their own viewports. The outer showcase
      // stays desktop-width so the iframe itself remains fully reachable.
      const frame = showcase.frameLocator("iframe");
      // One live dock per frame — never two instances of one owner.
      await expect(frame.locator("[data-attention-dock]")).toHaveCount(1);
      const drawer = frame.getByRole("dialog");
      if (!(await drawer.isVisible())) {
        await frame.locator(".dock-bar").first().click();
      }
      await expect(drawer).toBeVisible();
      const chip = frame.locator(".decision-chip").first();
      await expect(chip).toBeVisible();

      const option = frame
        .getByRole("button", { name: /Seen on canvas open/ })
        .first();
      const box = await option.boundingBox();
      expect(box!.height, `${label} touch target`).toBeGreaterThanOrEqual(44);

      // The frame's own media query, measured rather than asserted.
      const framed = page.frameLocator("iframe");
      const narrow = await framed
        .locator("body")
        .evaluate(() => matchMedia("(max-width: 620px)").matches);
      expect(narrow, `${label} media query`).toBe(true);

      const overflow = await framed
        .locator("body")
        .evaluate(
          () =>
            document.documentElement.scrollWidth -
            document.documentElement.clientWidth,
        );
      expect(overflow, `${label} horizontal overflow`).toBeLessThanOrEqual(1);

      // The opened chip must stay inside the viewport, top edge included.
      const top = await chip.evaluate((el) => el.getBoundingClientRect().top);
      expect(top, `${label} chip top`).toBeGreaterThanOrEqual(0);

      await shot(page, name);
      const close = frame.getByRole("button", { name: "Done", exact: true });
      const closeMetrics = await close.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return {
          top: box.top,
          right: box.right,
          bottom: box.bottom,
          left: box.left,
          width: innerWidth,
          height: innerHeight,
        };
      });
      expect(closeMetrics.top, `${label} close top`).toBeGreaterThanOrEqual(0);
      expect(closeMetrics.left, `${label} close left`).toBeGreaterThanOrEqual(0);
      expect(closeMetrics.right, `${label} close right`).toBeLessThanOrEqual(closeMetrics.width);
      expect(closeMetrics.bottom, `${label} close bottom`).toBeLessThanOrEqual(closeMetrics.height);
      await close.press("Enter");
      await expect(frame.locator(".dock-bar").first()).toBeVisible();
    }

    await showcase
      .getByRole("button", { name: "Desktop", exact: true })
      .click();
    const wide = await page.evaluate(
      () => matchMedia("(max-width: 620px)").matches,
    );
    expect(wide).toBe(false);

    await page.emulateMedia({ reducedMotion: "reduce" });
    const reduced = await page.evaluate(
      () => matchMedia("(prefers-reduced-motion: reduce)").matches,
    );
    expect(reduced).toBe(true);
  });
});

test.describe("@surface:attention Skip to the next decision", () => {
  for (const [name, size] of [
    ["desktop", { width: 1280, height: 800 }],
    ["mobile", { width: 393, height: 852 }],
  ] as const) {
    test(`keeps the collapsed row clear and skips from the opened decision at ${size.width}px`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(size);
      await page.goto("/_design#attention-dock");
      // The decision drawer is portaled to document.body, outside #attention-dock.
    const showcase = page;
      await showcase.getByRole("button", { name: "Many waiting" }).click();
      const bar = showcase.locator(".dock-bar").first();
      const skip = showcase.getByRole("button", {
        name: "Skip to next decision",
      });
      await expect(bar).toContainText("How much completed work");
      await expect(bar).toContainText("1 of 4");
      await expect(skip).toBeHidden();
      // The question keeps the full row at phone width.
      const titleWidth = await bar
        .locator(".dock-text b")
        .evaluate((el) => el.clientWidth);
      expect(titleWidth).toBeGreaterThanOrEqual(140);
      await saveEvidenceScreenshot(page, testInfo, `dock-skip-${name}-collapsed`, { fullPage: false });

      await bar.click();
      await expect(showcase.locator(".decision-chip")).toHaveCount(1);
      await expect(skip).toBeVisible();
      await saveEvidenceScreenshot(page, testInfo, `dock-skip-${name}-open`, { fullPage: false });

      await skip.focus();
      await expect(skip).toBeFocused();
      await page.keyboard.press("Enter");
      const next = showcase.getByRole("heading", {
        name: "Which provider should answer first?",
      });
      await expect(next).toBeVisible();
      // The Skip button is gone with the outgoing item; focus must land on the
      // next item, never fall back to the document body.
      await expect(next).toBeFocused();
      await expect(showcase.locator(".dock-bar")).toHaveCount(0);
    });

    test(`moves focus into the next item when skipping an opened review at ${size.width}px`, async ({
      page,
    }) => {
      await page.setViewportSize(size);
      await page.goto("/_design#attention-dock");
      // The decision drawer is portaled to document.body, outside #attention-dock.
    const showcase = page;
      await showcase
        .getByRole("button", { name: "Review, then decisions" })
        .click();
      await showcase.locator(".dock-bar").first().click();
      const review = showcase.getByRole("heading", {
        name: "Reviewer findings on the plans PR",
      });
      await expect(review).toBeFocused();

      const skip = showcase.getByRole("button", {
        name: "Skip to next decision",
      });
      await skip.focus();
      await page.keyboard.press("Enter");
      const next = showcase.getByRole("heading", {
        name: "How much completed work should the landing canvas retain?",
      });
      await expect(next).toBeVisible();
      await expect(next).toBeFocused();
    });
  }
});

test.describe("@surface:attention @surface:chat The dock in the real shell", () => {
  test("a subagent decision is answerable without leaving the conversation", async ({
    page,
    request,
  }, testInfo) => {
    await seedQueue(
      page,
      { external_id: "decision-1", renderer: "decision", detail: decision() },
      3,
    );
    await openThread(page, request, "attention-decision");

    // The composed first viewport: the dock sits above the real composer.
    const dock = page.locator("[data-attention-dock]");
    await expect(dock).toHaveCount(1);
    // The dock's live region repeats the title for screen readers; read the bar itself.
    const bar = dock.locator(".dock-bar");
    await expect(bar.getByText("Where should decision state live?")).toBeVisible();
    await expect(bar.getByText("Reviewer subagent")).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Message CORSIA" }),
    ).toBeVisible();
    await shot(page, "shell-01-chat-dock-above-composer");
    await saveEvidenceScreenshot(page, testInfo, "shell-chat-dock");

    // Explicit open in the drawer — never buried in the message stream.
    await dock.locator(".dock-bar").click();
    const drawer = page.getByRole("dialog", {
      name: "Decision — Where should decision state live?",
    });
    await expect(drawer).toBeVisible();
    await expect(page.locator(".decision-chip")).toHaveCount(1);
    await expect(page.getByText("P5 needs one canonical home")).toBeVisible();
    await expect(
      drawer.getByRole("button", { name: "Answer", exact: true }),
    ).toBeInViewport();
    await shot(page, "shell-02-chat-chip-open");
  });

  test("an unsent draft and the open chip survive a details-panel toggle", async ({
    page,
    request,
  }) => {
    await seedQueue(
      page,
      { external_id: "decision-1", renderer: "decision", detail: decision() },
      1,
    );
    await openThread(page, request, "attention-draft");

    await page.locator("[data-attention-dock] .dock-bar").click();
    await page.locator(".other-option textarea").fill("Half-typed answer");

    // The open drawer is modal and covers the header button, so use the global shortcut.
    await page.keyboard.press("ControlOrMeta+\\");
    await expect(page.locator(".chat-shell")).toHaveCount(1);

    // Exactly one dock, still open, draft intact.
    await expect(page.locator("[data-attention-dock]")).toHaveCount(1);
    await expect(page.locator(".decision-chip")).toHaveCount(1);
    await expect(page.locator(".other-option textarea")).toHaveValue(
      "Half-typed answer",
    );
    await shot(page, "shell-03-details-draft-preserved");
  });

  test("documents and settings deliberately have no dock, and returning restores it", async ({
    page,
    request,
  }) => {
    await seedQueue(
      page,
      { external_id: "decision-1", renderer: "decision", detail: decision() },
      1,
    );
    const title = await openThread(page, request, "attention-settings");
    await expect(page.locator("[data-attention-dock]")).toHaveCount(1);

    const narrow = (page.viewportSize()?.width ?? 0) <= 768;
    // On a phone the global rail lives in the sidebar drawer.
    if (narrow) await page.getByRole("button", { name: "Toggle sidebar" }).click();
    await page.locator('button[title="Settings"]').first().click();
    await expect(page.getByRole("button", { name: "Providers" })).toBeVisible();
    await expect(page.locator("[data-attention-dock]")).toHaveCount(0);
    await shot(page, "shell-04-settings-no-dock");

    // Closing global settings lands on Activity; the dock returns with the conversation.
    await page
      .getByRole("button", { name: narrow ? "Back" : "Close settings", exact: true })
      .first()
      .click();
    await selectThread(page, title);
    await expect(page.locator("[data-attention-dock]")).toHaveCount(1);
  });
});

test.describe("@surface:attention Hit testing — the dock is actually reachable", () => {
  for (const [label, width, height] of [
    ["desktop", 1100, 800],
    ["mobile-390", 390, 844],
    ["mobile-320", 320, 720],
  ] as const) {
    test(`the collapsed bar and the opened Answer are hit-testable at ${label}`, async ({
      page,
      request,
    }) => {
      await page.setViewportSize({ width, height });
      await seedQueue(
        page,
        { external_id: "decision-1", renderer: "decision", detail: decision() },
        2,
      );
      await openThread(page, request, `attention-hit-${label}`);

      // The whole point of the slot owner: `elementFromPoint` must land on
      // the dock, not on the floating composer that covers the same band.
      const bar = page.locator("[data-attention-dock] .dock-bar");
      await expect(bar).toBeVisible();
      const barBox = (await bar.boundingBox())!;
      const hit = await page.evaluate(
        ([x, y]) => {
          const el = document.elementFromPoint(x, y);
          return el?.closest("[data-attention-dock]")
            ? "dock"
            : (el?.tagName ?? "none");
        },
        [barBox.x + barBox.width / 2, barBox.y + barBox.height / 2],
      );
      expect(hit, `${label} collapsed bar`).toBe("dock");

      await bar.click();
      const drawer = page.getByRole("dialog", {
        name: "Decision — Where should decision state live?",
      });
      await expect(drawer).toBeVisible();
      const answer = drawer.getByRole("button", {
        name: "Answer",
        exact: true,
      });
      await expect(answer).toBeInViewport();
      await answer.scrollIntoViewIfNeeded();
      const answerBox = (await answer.boundingBox())!;
      expect(
        answerBox.y,
        `${label} Answer above the fold`,
      ).toBeGreaterThanOrEqual(0);
      const answerHit = await page.evaluate(
        ([x, y]) => {
          const el = document.elementFromPoint(x, y);
          return el?.closest("[data-adaptive-drawer]")
            ? "drawer"
            : (el?.tagName ?? "none");
        },
        [answerBox.x + answerBox.width / 2, answerBox.y + answerBox.height / 2],
      );
      expect(answerHit, `${label} Answer`).toBe("drawer");
    });
  }

  test("an empty queue reserves no message-list band", async ({
    page,
    request,
  }) => {
    await seedQueue(page, null, 0);
    await openThread(page, request, "attention-empty");

    await expect(page.locator("[data-attention-dock]")).toHaveCount(0);
    const children = await page
      .locator(".chat-attention-stack")
      .evaluate((el) => el.children.length);
    expect(children).toBe(0);
  });
});
