import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot } from '../support/corsia';
import type { Locator } from '@playwright/test';

// The color of the pane's drawn page at a fraction of the canvas, in CSS pixels.
function pixelAt(canvas: Locator, fx: number, fy: number) {
	return canvas.evaluate((node, [fx, fy]) => {
		const canvas = node as HTMLCanvasElement;
		const data = canvas.getContext('2d')!.getImageData(Math.floor(canvas.width * fx), Math.floor(canvas.height * fy), 1, 1).data;
		return Array.from(data.slice(0, 3));
	}, [fx, fy]);
}

// Frames are JPEG, so a drawn color is matched within a small tolerance.
async function colorAt(canvas: Locator, fx: number, fy: number, rgb: number[]) {
	const pixel = await pixelAt(canvas, fx, fy);
	return pixel.every((value, index) => Math.abs(value - rgb[index]) <= 12) ? rgb : pixel;
}

test.describe('@flow:browser-pane shared browser pane', () => {
	test('opens a browser tab and hands control to the user', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'desktop-only first cut');
		const title = `browser-pane-${Date.now()}`;
		const threadId = await createSession(request, title);
		const browserSockets: string[] = [];
		const browserFrames: string[] = [];
		page.on('websocket', (socket) => {
			browserSockets.push(socket.url());
			socket.on('framereceived', (frame) => {
				try { browserFrames.push(`${socket.url()}:${String(frame.payload)}`); } catch { /* non-JSON frames are irrelevant */ }
			});
		});
		await loginByUrl(page);
		await page.getByText(title).first().click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await expect(address).toBeVisible();
		await address.fill(`${new URL(page.url()).origin}/browser-pane-probe.html`);
		const opened = page.waitForResponse((response) => response.url().includes(`/api/browser/${threadId}/open`));
		await page.getByRole('button', { name: 'Go' }).click();
		const openResponse = await opened;
		expect(openResponse.status(), await openResponse.text()).toBe(200);
		await expect.poll(() => browserSockets.filter((url) => url.includes(`/api/browser/${threadId}/stream`)).length).toBeGreaterThanOrEqual(2);
		await expect.poll(() => browserFrames.some((entry) => entry.includes('"type":"frame"')), { message: JSON.stringify(browserFrames) }).toBe(true);
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect.poll(() => canvas.evaluate((node) => {
			const canvas = node as HTMLCanvasElement;
			return canvas.width > 0 && canvas.height > 0;
		})).toBe(true);
		// The page takes the pane's size, so it fills the canvas edge to edge with no letterbox.
		const bounds = await canvas.boundingBox();
		await expect.poll(() => {
			const last = browserFrames.filter((entry) => entry.includes('"type":"frame"')).at(-1) ?? '';
			return /"deviceWidth":(\d+)/.exec(last)?.[1];
		}, { timeout: 15_000 }).toBe(String(Math.round(bounds!.width)));
		await expect.poll(() => colorAt(canvas, 0.02, 0.02, [240, 240, 240])).toEqual([240, 240, 240]);
		await expect.poll(() => colorAt(canvas, 0.98, 0.98, [240, 240, 240])).toEqual([240, 240, 240]);
		// The page follows CORSIA's theme: auto resolves to dark here, so the page turns dark.
		await page.emulateMedia({ colorScheme: 'dark' });
		await expect.poll(() => colorAt(canvas, 0.02, 0.02, [30, 30, 30]), { timeout: 15_000 }).toEqual([30, 30, 30]);
		await expect(page.getByRole('button', { name: 'Take over' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-watch-only');
		await page.getByRole('button', { name: 'Take over' }).click();
		await expect.poll(() => browserFrames.some((entry) => entry.includes('"type":"control"'))).toBe(true);
		await expect(page.getByText('You have control. The agent waits')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-control');
		// A click lands where it is drawn, and typing reaches the field it focused.
		const controlled = (await canvas.boundingBox())!;
		await canvas.click({ position: { x: controlled.width / 2, y: controlled.height * 0.25 } });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [0, 200, 0])).toEqual([0, 200, 0]);
		await canvas.click({ position: { x: controlled.width / 2, y: controlled.height * 0.75 } });
		await page.keyboard.type('hi');
		await expect.poll(() => colorAt(canvas, 0.9, 0.9, [0, 0, 200])).toEqual([0, 0, 200]);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-input');
		// Enter in the address bar navigates through the holding stream and keeps control.
		await address.fill(`${new URL(page.url()).origin}/browser-pane-probe.html?navigated`);
		await address.press('Enter');
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [120, 0, 160])).toEqual([120, 0, 160]);
		await expect(page.getByText('You have control. The agent waits')).toBeVisible();
		// Back returns to the first page (restored as clicked from the back-forward cache, or reloaded dark);
		// Forward and Reload reach the page too.
		await page.getByRole('button', { name: 'Back', exact: true }).click();
		const firstPage = [[0, 200, 0], [30, 30, 30]];
		await expect.poll(async () => {
			const seen = await Promise.all(firstPage.map((rgb) => colorAt(canvas, 0.1, 0.1, rgb)));
			return seen.some((pixel, index) => pixel === firstPage[index]);
		}).toBe(true);
		await page.getByRole('button', { name: 'Forward', exact: true }).click();
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [120, 0, 160])).toEqual([120, 0, 160]);
		await page.getByRole('button', { name: 'Reload', exact: true }).click();
		await expect.poll(() => browserFrames.filter((entry) => entry.includes('"type":"navigate_result"')).length).toBe(4);
		await expect.poll(() => browserFrames.some((entry) => entry.includes('"type":"navigate_result","error":"'))).toBe(false);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-navigation');
		await page.getByRole('button', { name: 'Hand back' }).click();
		await expect(page.getByText('Agent is driving. Watch only')).toBeVisible();
		await page.getByRole('button', { name: 'Close Browser' }).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		await expect(canvas).toBeVisible();
	});
});
