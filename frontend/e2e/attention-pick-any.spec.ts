import { expect, test } from "@playwright/test";

/**
 * Pick-any decisions in a real browser, through the production dock and the
 * `/_design` showcase (only the network is stubbed). Desktop and 393 px.
 */

const FRAME = "/_design?frame=attention-dock&scenario=pick-any";

for (const [label, width, height] of [
  ["desktop", 1440, 1000],
  ["393", 393, 852],
] as const) {
  test(`@surface:attention pick any — choose several, note, answer (${label})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await page.goto(FRAME);

    const bar = page.locator(".dock-bar").first();
    await expect(bar).toBeVisible();
    if (!(await page.locator(".decision-chip").first().isVisible())) {
      await bar.click();
    }
    const chip = page.locator(".decision-chip").first();
    await expect(chip).toBeVisible();

    const group = chip.getByRole("group", { name: "Choose any that apply" });
    await expect(group).toBeVisible();
    const boxes = group.getByRole("checkbox");
    await expect(boxes).toHaveCount(3);

    // The recommendation starts ticked.
    await expect(boxes.nth(0)).toBeChecked();
    await expect(boxes.nth(1)).toBeChecked();
    await expect(boxes.nth(2)).not.toBeChecked();
    await expect(chip.locator(".receipt")).toContainText(
      "2 of 3 chosen · as recommended",
    );

    // Each row meets the touch-target floor on a phone.
    if (width <= 720) {
      for (let i = 0; i < 3; i += 1) {
        const row = await boxes.nth(i).locator("xpath=ancestor::label").boundingBox();
        expect(row!.height, `row ${i}`).toBeGreaterThanOrEqual(44);
      }
    }
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);

    // Keyboard toggles a box; a note never unticks anything.
    await boxes.nth(2).focus();
    await page.keyboard.press("Space");
    await expect(boxes.nth(2)).toBeChecked();
    await chip.locator(".other-option textarea").fill("search later");
    await expect(boxes.nth(0)).toBeChecked();
    await expect(boxes.nth(2)).toBeChecked();

    // Untick everything: answering with none is allowed.
    for (let i = 0; i < 3; i += 1) await boxes.nth(i).uncheck();
    await expect(
      chip.getByRole("button", { name: "Answer", exact: true }),
    ).toBeEnabled();

    await chip.getByRole("button", { name: "Answer", exact: true }).click();
    // The showcase stubs fetch in-page, so it records the body it received.
    await page.waitForFunction(
      () => (window as unknown as { __showcaseAnswer?: unknown }).__showcaseAnswer,
    );
    const sent = await page.evaluate(
      () => (window as unknown as { __showcaseAnswer: Record<string, unknown> }).__showcaseAnswer,
    );
    expect(sent.option_ids).toEqual([]);
    expect(sent.other_text).toBe("search later");
    expect(sent).not.toHaveProperty("option_id");
  });
}
