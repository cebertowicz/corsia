import { test } from "./support/test";
import { expect, type Page, type TestInfo } from "@playwright/test";
import path from "node:path";

async function waitForDrawerToSettle(page: Page) {
  const drawer = page.locator("[data-adaptive-drawer]");
  await expect(drawer).toBeVisible();
  await drawer.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    await Promise.all(
      element
        .getAnimations({ subtree: true })
        .map((animation) => animation.finished.catch(() => undefined)),
    );
  });
  await expect
    .poll(() => drawer.evaluate((element) => element.getAnimations({ subtree: true }).length))
    .toBe(0);
}

async function openDecision(page: Page) {
  await page.goto("/_design?frame=attention-dock&scenario=one");
  const bar = page.locator(".dock-bar");
  await expect(bar).toBeVisible();
  await bar.click();
  await expect(page.locator(".decision-chip")).toBeVisible();
  await waitForDrawerToSettle(page);
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
  const directory = process.env.PE_FRAME_DIR ?? testInfo.outputDir;
  await page.screenshot({
    path: path.join(directory, `${name}.png`),
    fullPage: true,
    animations: "disabled",
  });
}

for (const [name, width, height, colorScheme] of [
  ["393-light", 393, 852, "light"],
  ["393-dark", 393, 852, "dark"],
  ["1440-light", 1440, 900, "light"],
] as const) {
  test(`@surface:attention decision footer remains inside its scroll region (${name})`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "chromium-desktop", "Frames set their exact viewport directly.");
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
    await openDecision(page);

    const geometry = await page.locator(".drawer-body").evaluate((scrollRegion) => {
      const footer = document.querySelector<HTMLElement>(".decision-actions");
      const sheet = document.querySelector<HTMLElement>(".drawer-panel");
      const decisionBody = document.querySelector<HTMLElement>(".decision-body");
      const options = Array.from(document.querySelectorAll<HTMLElement>(".option-button"));
      if (!footer || !sheet || !decisionBody) throw new Error("Decision drawer geometry is incomplete");

      scrollRegion.scrollTop = scrollRegion.scrollHeight;
      const scrollRect = scrollRegion.getBoundingClientRect();
      const footerRect = footer.getBoundingClientRect();
      const scrollStyle = getComputedStyle(scrollRegion);
      const decisionBodyStyle = getComputedStyle(decisionBody);
      const footerStyle = getComputedStyle(footer);
      const coveredOptions = options
        .map((option) => ({ label: option.textContent?.trim(), rect: option.getBoundingClientRect() }))
        .filter(({ rect }) => rect.bottom > footerRect.top && rect.top < footerRect.bottom)
        .map(({ label }) => label);

      return {
        sheetBottom: sheet.getBoundingClientRect().bottom,
        scrollBottom: scrollRect.bottom,
        footerBottom: footerRect.bottom,
        coveredOptions,
        styles: {
          scrollPaddingBottom: scrollStyle.paddingBottom,
          decisionBodyPaddingBottom: decisionBodyStyle.paddingBottom,
          footerBottom: footerStyle.bottom,
          footerMarginTop: footerStyle.marginTop,
          footerPosition: footerStyle.position,
        },
      };
    });

    await capture(page, testInfo, `decision-footer-${name}`);
    expect(geometry.footerBottom, JSON.stringify(geometry)).toBeCloseTo(geometry.scrollBottom, 0);
    expect(geometry.footerBottom).toBeLessThanOrEqual(geometry.sheetBottom);
    expect(geometry.coveredOptions).toEqual([]);
  });
}

// 336px is a phone keyboard; 60px is the accessory-bar inset the app sets on focus.
for (const [name, keyboard] of [
  ["keyboard-closed", 0],
  ["keyboard-open", 336],
] as const) {
  test(`@surface:attention typing in Other keeps the whole box above the actions row (${name})`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "chromium-desktop", "Frames set their exact viewport directly.");
    await page.setViewportSize({ width: 393, height: 852 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openDecision(page);
    if (keyboard) {
      await page.setViewportSize({ width: 393, height: 852 - keyboard });
      await page.evaluate(() => document.documentElement.style.setProperty("--kb-toolbar-inset", "60px"));
    }

    const other = page.locator(".other-option textarea");
    // Focus and type like a phone does: Playwright's click would scroll the box clear of the row on its own.
    await other.focus();
    await page.keyboard.type(
      "Move it to the bottom, A, but also note this problem should go to the owner and be fixed properly so it does not come back next week.",
    );
    const geometry = await page.evaluate(() => {
      const box = document.querySelector<HTMLElement>(".other-option textarea")?.getBoundingClientRect();
      const footer = document.querySelector<HTMLElement>(".decision-actions")?.getBoundingClientRect();
      if (!box || !footer) throw new Error("Decision drawer geometry is incomplete");
      return { boxTop: box.top, boxBottom: box.bottom, footerTop: footer.top };
    });

    await capture(page, testInfo, `decision-other-${name}`);
    expect(geometry.boxBottom, JSON.stringify(geometry)).toBeLessThanOrEqual(geometry.footerTop);
    expect(geometry.boxTop, JSON.stringify(geometry)).toBeGreaterThanOrEqual(0);
  });
}
