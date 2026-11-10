import { test, expect } from '../support/test';

for (const [width, height] of [[1440, 900], [900, 900], [393, 852], [393, 420], [393, 350]] as const) {
 for (const theme of ['light', 'dark'] as const) {
  test(`decision entry stays visible at ${width}x${height} ${theme}`, async ({ page }, info) => {
   test.skip(info.project.name !== 'chromium-desktop');
   await page.setViewportSize({ width, height: Math.max(height, 852) });
   await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
   await page.goto('/_design?frame=attention-dock&scenario=one');
   await page.locator('.dock-bar').click();
   const drawer = page.locator('[data-adaptive-drawer]');
   await expect(drawer).toBeVisible();
   await drawer.evaluate(async element => {
    await Promise.all(element.getAnimations({ subtree: true }).map(animation => animation.finished.catch(() => undefined)));
   });
   const input = page.locator('.other-option textarea');
   await input.focus();
   await page.setViewportSize({ width, height });
   for (let i = 0; i < 8; i++) {
    await input.pressSequentially(`typed line ${i}`);
    if (i < 7) await input.press('Shift+Enter');
   }
   await page.screenshot({ path: info.outputPath('decision-typed.png'), animations: 'disabled' });
   await input.fill(Array.from({ length: 40 }, (_, i) => `decision line ${i}`).join('\n'));
   await expect.poll(async () => {
    const field = (await input.boundingBox())!;
    const actions = (await page.locator('.decision-actions').boundingBox())!;
    return field.y >= 0 && field.y + field.height <= actions.y && actions.y + actions.height <= height;
   }).toBe(true);
   await page.screenshot({ path: info.outputPath('decision-long.png'), animations: 'disabled' });
  });
 }
}
