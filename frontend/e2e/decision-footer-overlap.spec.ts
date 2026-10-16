import { expect, test, type Page, type TestInfo } from "@playwright/test";
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
