import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot } from '../support/corsia';

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
		const canvasState = await canvas.evaluate((node) => {
			const canvas = node as HTMLCanvasElement;
			const context = canvas.getContext('2d')!;
			const pixel = context.getImageData(Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), 1, 1).data;
			const bounds = canvas.getBoundingClientRect();
			return { width: canvas.width, height: canvas.height, bounds: [bounds.width, bounds.height], pixel: Array.from(pixel) };
		});
		expect(canvasState.pixel.some(Boolean), JSON.stringify(canvasState)).toBe(true);
		await expect(page.getByRole('button', { name: 'Take over' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-watch-only');
		await page.getByRole('button', { name: 'Take over' }).click();
		await expect.poll(() => browserFrames.some((entry) => entry.includes('"type":"control"'))).toBe(true);
		await expect(page.getByText('You have control. The agent waits')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-control');
		await page.getByRole('button', { name: 'Hand back' }).click();
		await expect(page.getByText('Agent is driving. Watch only')).toBeVisible();
		await page.getByRole('button', { name: 'Close Browser' }).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		await expect(canvas).toBeVisible();
	});
});
