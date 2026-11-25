import { test } from './support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread } from './support/corsia';
import { colorAt } from './support/browserCanvas';
import { startProbeServer } from './support/probeServer';

async function openPane(page: Page, request: Parameters<typeof createSession>[0], origin: string) {
 const title = `phone-typing-${Date.now()}-${Math.random()}`;
 await createSession(request, title);
 await loginByUrl(page);
 await page.getByRole('button', { name: 'Toggle sidebar' }).click();
 await sidebarThread(page, title).click();
 await page.getByRole('button', { name: 'Toggle panel' }).click();
 await page.getByRole('button', { name: 'Open browser', exact: true }).click();
 const address = page.getByRole('textbox', { name: 'Browser address' });
 await address.fill(`${origin}/browser-pane-probe.html?phone-signin`);
 const opened = page.waitForResponse(response => response.url().includes('/open') && response.url().includes('/api/browser/'));
 await address.press('Enter');
 expect((await opened).status()).toBe(200);
 const canvas = page.locator('canvas[aria-label="Shared browser"]');
 await expect.poll(() => colorAt(canvas, 0.75, 0.45, [240, 240, 240]), { timeout: 20_000 }).toEqual([240, 240, 240]);
 return canvas;
}

test.describe('phone typing in the shared browser', () => {
 test.use({ viewport: { width: 393, height: 852 } });
 let probe: Awaited<ReturnType<typeof startProbeServer>>;
 test.describe.configure({ timeout: 120_000 });
 test.beforeAll(async () => { probe = await startProbeServer(); });
 test.afterAll(async () => { await probe.close(); });

 test('touch field opens an input and forwards text to the page', async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-mobile', 'touch-viewer journey');
  const fields = new Promise<void>(resolve => page.on('websocket', socket => socket.on('framereceived', frame => {
   if (typeof frame.payload !== 'string') return;
   try { const message = JSON.parse(frame.payload); if (message.type === 'text_fields' && message.fields.length) resolve(); } catch { /* frame is not JSON */ }
  })));
  const canvas = await openPane(page, request, probe.origin);
  await fields;
  const box = (await canvas.boundingBox())!;
  await canvas.tap({ position: { x: box.width / 2, y: box.height * 0.45 } });
  const input = page.locator('[data-pane-field]');
  await expect(input).toBeFocused();
  await expect(input).toHaveAttribute('inputmode', 'email');
  await page.keyboard.insertText('hi');
  await expect(input).toHaveValue('');
  await expect.poll(() => colorAt(canvas, 0.75, 0.45, [180, 225, 255]), { timeout: 20_000 }).toEqual([180, 225, 255]);
  if (process.env.PHONE_SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.PHONE_SCREENSHOT_DIR}/after.png` });
  await input.tap();
  await page.keyboard.insertText('!');
  await expect.poll(() => colorAt(canvas, 0.75, 0.45, [180, 255, 180]), { timeout: 20_000 }).toEqual([180, 255, 180]);
  await page.keyboard.press('Backspace');
  await expect.poll(() => colorAt(canvas, 0.75, 0.45, [180, 225, 255]), { timeout: 20_000 }).toEqual([180, 225, 255]);
 });

 test('baseline: the canvas receives focus when touch field discovery is off', async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-mobile', 'touch-viewer journey');
  await page.addInitScript(() => {
   const match = window.matchMedia.bind(window);
   window.matchMedia = (query: string) => query === '(pointer: coarse)' ? { ...match(query), matches: false } : match(query);
  });
  const canvas = await openPane(page, request, probe.origin);
  const box = (await canvas.boundingBox())!;
  await canvas.tap({ position: { x: box.width / 2, y: box.height * 0.45 } });
  await expect(canvas).toBeFocused();
  await expect(page.locator('[data-pane-field]')).toHaveCount(0);
  if (process.env.PHONE_SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.PHONE_SCREENSHOT_DIR}/before.png` });
  await page.keyboard.type('hi');
  await expect.poll(() => colorAt(canvas, 0.75, 0.45, [180, 225, 255]), { timeout: 20_000 }).toEqual([180, 225, 255]);
 });
});
