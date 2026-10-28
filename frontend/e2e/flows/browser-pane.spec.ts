import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, sidebarThread } from '../support/corsia';
import type { Locator } from '@playwright/test';
import { startProbeServer } from '../support/probeServer';
import { seedMockResponses } from '../support/mock';
import { sendChatMessage } from '../support/corsia';

// Frames are JPEG, so a drawn color is matched within a small tolerance.
async function colorAt(canvas: Locator, fx: number, fy: number, rgb: number[]) {
	const pixel = await canvas.evaluate((node, [fx, fy]) => {
		const frame = node as HTMLCanvasElement;
		return Array.from(frame.getContext('2d')!.getImageData(Math.floor(frame.width * fx), Math.floor(frame.height * fy), 1, 1).data.slice(0, 3));
	}, [fx, fy]);
	return pixel.every((value, index) => Math.abs(value - rgb[index]) <= 12) ? rgb : pixel;
}

test.describe('@flow:browser-pane shared browser pane', () => {
	let probe: Awaited<ReturnType<typeof startProbeServer>>;
	test.beforeAll(async () => { probe = await startProbeServer(); });
	test.afterAll(async () => { await probe.close(); });

	test('follows a page-opened popup and returns when it closes', async ({ page, request }, testInfo) => {
		const title = `browser-popup-${Date.now()}`;
		await createSession(request, title);
		await loginByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		if ((page.viewportSize()?.width ?? 0) <= 1024) await page.getByRole('button', { name: 'Toggle panel' }).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		const compact = (page.viewportSize()?.width ?? 0) <= 1024;
		await address.fill(`${probe.origin}/browser-pane-probe.html?popup-opener${compact ? '&timed' : ''}`);
		const opened = page.waitForResponse(response => response.url().includes('/open') && response.url().includes('/api/browser/'));
		await address.press('Enter');
		expect((await opened).status()).toBe(200);
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [240, 240, 240])).toEqual([240, 240, 240]);
		await saveEvidenceScreenshot(page, testInfo, 'popup-baseline-before');
		const prior = probe.popupCount();
		const bounds = (await canvas.boundingBox())!;
		if (!compact) await canvas.click({ position: { x: bounds.width / 2, y: bounds.height / 4 } });
		await expect.poll(() => probe.popupCount(), { timeout: 15_000 }).toBeGreaterThan(prior);
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [200, 0, 0]), { timeout: 15_000 }).toEqual([200, 0, 0]);
		await saveEvidenceScreenshot(page, testInfo, 'popup-followed');
		if (!compact) await canvas.click({ position: { x: bounds.width / 2, y: bounds.height / 4 } });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [240, 240, 240]), { timeout: 15_000 }).toEqual([240, 240, 240]);
		await saveEvidenceScreenshot(page, testInfo, 'popup-closed');
	});

	test('follows a popup in dark theme', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name === 'chromium-mobile', 'desktop proof');
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'dark'));
		const title = `popup-dark-${Date.now()}`;
		await createSession(request, title);
		await loginByUrl(page);
		await sidebarThread(page, title).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await address.fill(`${probe.origin}/browser-pane-probe.html?popup-opener`);
		const opened = page.waitForResponse(response => response.url().includes('/open') && response.url().includes('/api/browser/'));
		await address.press('Enter');
		expect((await opened).status()).toBe(200);
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [30, 30, 30]), { timeout: 15_000 }).toEqual([30, 30, 30]);
		await saveEvidenceScreenshot(page, testInfo, 'dark-popup-before');
		const bounds = (await canvas.boundingBox())!;
		await canvas.click({ position: { x: bounds.width / 2, y: bounds.height / 4 } });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [200, 0, 0]), { timeout: 15_000 }).toEqual([200, 0, 0]);
		await saveEvidenceScreenshot(page, testInfo, 'dark-popup-followed');
	});

	test('a person step follows a popup and settles after navigation', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name === 'chromium-mobile', 'desktop proof');
		test.setTimeout(90_000);
		const title = `popup-step-${Date.now()}`;
		const thread = await createSession(request, title);
		await loginByUrl(page);
		await sidebarThread(page, title).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await address.fill(`${probe.origin}/browser-pane-probe.html?popup-opener`);
		const opened = page.waitForResponse(response => response.url().includes('/open') && response.url().includes('/api/browser/'));
		await address.press('Enter');
		expect((await opened).status()).toBe(200);
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [240, 240, 240])).toEqual([240, 240, 240]);
		const bounds = (await canvas.boundingBox())!;
		await canvas.click({ position: { x: bounds.width / 2, y: bounds.height / 4 } });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [200, 0, 0]), { timeout: 15_000 }).toEqual([200, 0, 0]);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'popup-person-step', name: 'browser_person_step', arguments: JSON.stringify({ instruction: 'Complete this popup.' }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Waiting.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Ask me to complete this popup.');
		await expect(page.getByRole('button', { name: /Do: Your turn in the shared browser/ })).toBeVisible({ timeout: 30_000 });
		await saveEvidenceScreenshot(page, testInfo, 'popup-person-step');
		await address.fill(`${probe.origin}/browser-pane-probe.html?navigated`);
		await address.press('Enter');
		await expect(page.getByRole('button', { name: /Do: Your turn in the shared browser/ })).toHaveCount(0, { timeout: 20_000 });
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [120, 0, 160]), { timeout: 15_000 }).toEqual([120, 0, 160]);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'list-tabs', name: 'browser_snapshot', arguments: JSON.stringify({ list_tabs: true }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ tool_calls: [{ id: 'switch-tab', name: 'browser_snapshot', arguments: JSON.stringify({ tab: 't1' }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Switched.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'List the tabs and switch to the first.');
		await expect.poll(() => colorAt(canvas, 0.1, 0.1, [240, 240, 240]), { timeout: 30_000 }).toEqual([240, 240, 240]);
		await saveEvidenceScreenshot(page, testInfo, 'agent-switched-opener');
	});

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
		await sidebarThread(page, title).click();
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await expect(address).toBeVisible();
		await address.fill(`${probe.origin}/browser-pane-probe.html`);
		const opened = page.waitForResponse(response => response.url().includes(`/api/browser/${threadId}/open`));
		await address.press('Enter');
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
		// A ready page needs no status: the address field and the page are the whole toolbar.
		await expect(address).toHaveValue(/^127\.0\.0\.1/);
		await expect(page.getByRole('button', { name: 'Go' })).toHaveCount(0);
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

		// Enter in the address bar navigates through the open stream while the page stays shared.
		await address.fill(`${probe.origin}/browser-pane-probe.html?navigated`);
		await address.press('Enter');
		await expect.poll(() => browserFrames.some(entry => entry.includes('"type":"navigate_result","error":null'))).toBe(true);
		await expect(page.getByText('Opening', { exact: true })).toHaveCount(0);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-navigation');

		// The person's Meta+A reaches Chrome with its modifier bit, so this replaces the existing value and submits.
		const navigationCount = browserFrames.filter(entry => entry.includes('"type":"navigate_result"')).length;
		const frameCount = browserFrames.filter(entry => entry.includes('"type":"frame"')).length;
		await address.fill(`${probe.origin}/browser-pane-probe.html?password`);
		await address.press('Enter');
		await expect.poll(() => browserFrames.filter(entry => entry.includes('"type":"navigate_result"')).length).toBeGreaterThan(navigationCount);
		await expect.poll(() => browserFrames.filter(entry => entry.includes('"type":"frame"')).length).toBeGreaterThan(frameCount);
		await canvas.click({ position: { x: shared.width / 2, y: shared.height * 0.25 } });
		await page.keyboard.press('Meta+A');
		await page.keyboard.type('next');
		await page.keyboard.press('Enter');
		await expect.poll(() => colorAt(canvas, 0.5, 0.5, [0, 160, 80])).toEqual([0, 160, 80]);
		await saveEvidenceScreenshot(page, testInfo, 'browser-pane-password-submitted');
		// The page's own submit navigated it, and the tab label followed that main-frame navigation.
		await page.getByRole('button', { name: /^Close .*browser-pane-probe\.html\?submitted$/ }).click();
	});
});
