import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot } from '../support/corsia';
import type { Locator } from '@playwright/test';

// Frames are JPEG, so a drawn color is matched within a small tolerance.
async function colorAt(canvas: Locator, fx: number, fy: number, rgb: number[]) {
	const pixel = await canvas.evaluate((node, [fx, fy]) => {
		const frame = node as HTMLCanvasElement;
		return Array.from(frame.getContext('2d')!.getImageData(Math.floor(frame.width * fx), Math.floor(frame.height * fy), 1, 1).data.slice(0, 3));
	}, [fx, fy]);
	return pixel.every((value, index) => Math.abs(value - rgb[index]) <= 12) ? rgb : pixel;
}

test.describe('@flow:browser-pane shared browser pane', () => {
	test('opens a shared browser tab and relays person input', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'desktop-only first cut');
		const title = `browser-pane-${Date.now()}`;
		const threadId = await createSession(request, title);
		const browserSockets: string[] = [];
		const browserFrames: string[] = [];
		page.on('websocket', socket => {
			browserSockets.push(socket.url());
			socket.on('framereceived', frame => browserFrames.push(`${socket.url()}:${String(frame.payload)}`));
		});
		await loginByUrl(page);
		await page.getByText(title).first().click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await expect(address).toBeVisible();
		await address.fill(`${new URL(page.url()).origin}/browser-pane-probe.html`);
		const opened = page.waitForResponse(response => response.url().includes(`/api/browser/${threadId}/open`));
		await page.getByRole('button', { name: 'Go' }).click();
		const openResponse = await opened;
		expect(openResponse.status(), await openResponse.text()).toBe(200);
		await expect.poll(() => browserSockets.filter(url => url.includes(`/api/browser/${threadId}/stream`)).length).toBeGreaterThanOrEqual(2);
		await expect.poll(() => browserFrames.some(entry => entry.includes('"type":"frame"')), { message: JSON.stringify(browserFrames) }).toBe(true);
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect.poll(() => canvas.evaluate(node => {
			const frame = node as HTMLCanvasElement;
			return frame.width > 0 && frame.height > 0;
		})).toBe(true);
		const bounds = await canvas.boundingBox();
		await expect.poll(() => {
			const last = browserFrames.filter(entry => entry.includes('"type":"frame"')).at(-1) ?? '';
			return /"deviceWidth":(\d+)/.exec(last)?.[1];
		}, { timeout: 15_000 }).toBe(String(Math.round(bounds!.width)));
		await expect.poll(() => colorAt(canvas, 0.02, 0.02, [240, 240, 240])).toEqual([240, 240, 240]);
		await expect(page.getByText('Shared with the agent')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Take over' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Hand back' })).toHaveCount(0);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-shared');

		// A click lands where it is drawn and typing reaches the field it focused without a control hand-off.
		const shared = (await canvas.boundingBox())!;
		await canvas.click({ position: { x: shared.width / 2, y: shared.height * 0.25 } });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [0, 200, 0])).toEqual([0, 200, 0]);
		await canvas.click({ position: { x: shared.width / 2, y: shared.height * 0.75 } });
		await page.keyboard.type('hi');
		await expect.poll(() => colorAt(canvas, 0.9, 0.9, [0, 0, 200])).toEqual([0, 0, 200]);

		// Enter in the address bar navigates through the holding stream while the page stays shared.
		await address.fill(`${new URL(page.url()).origin}/browser-pane-probe.html?navigated`);
		await address.press('Enter');
		await expect.poll(() => browserFrames.some(entry => entry.includes('"type":"navigate_result","error":null'))).toBe(true);
		await expect(page.getByText('Shared with the agent')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-navigation');

		// The person's Meta+A reaches Chrome with its modifier bit, so this replaces the existing value and submits.
		const navigationCount = browserFrames.filter(entry => entry.includes('"type":"navigate_result"')).length;
		const frameCount = browserFrames.filter(entry => entry.includes('"type":"frame"')).length;
		await address.fill(`${new URL(page.url()).origin}/browser-pane-probe.html?password`);
		await address.press('Enter');
		await expect.poll(() => browserFrames.filter(entry => entry.includes('"type":"navigate_result"')).length).toBeGreaterThan(navigationCount);
		await expect.poll(() => browserFrames.filter(entry => entry.includes('"type":"frame"')).length).toBeGreaterThan(frameCount);
		await canvas.click({ position: { x: shared.width / 2, y: shared.height * 0.25 } });
		await page.keyboard.press('Meta+A');
		await page.keyboard.type('next');
		await page.keyboard.press('Enter');
		await expect.poll(() => colorAt(canvas, 0.5, 0.5, [0, 160, 80])).toEqual([0, 160, 80]);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-password-submitted');
		await page.getByRole('button', { name: 'Close Browser' }).click();
	});
});
