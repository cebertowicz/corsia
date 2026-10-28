import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, sidebarThread, saveEvidenceScreenshot } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { startProbeServer } from '../support/probeServer';

test('headless browser is revealed by a person step', async ({ page, request }, info) => {
	test.skip(info.project.name === 'chromium-mobile', 'desktop proof');
	test.setTimeout(90_000);
	const probe = await startProbeServer();
	try {
		const title = `share-proof-${Date.now()}`;
		const thread = await createSession(request, title);
		await loginByUrl(page);
		await sidebarThread(page, title).click();
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'open-headless', name: 'browser_open', arguments: JSON.stringify({ url: `${probe.origin}/browser-pane-probe.html` }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Headless ready.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Open the fixture.');
		await expect(page.getByRole('button', { name: /Expand Worked/ })).toBeVisible({ timeout: 30_000 });
		await page.getByRole('button', { name: /Expand Worked/ }).click();
		await expect(page.getByText('Headless ready.')).toBeVisible();
		await expect(page.locator('canvas[aria-label="Shared browser"]')).toHaveCount(0);
		await saveEvidenceScreenshot(page, info, 'headless-no-pane');
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'person-step', name: 'browser_person_step', arguments: JSON.stringify({ instruction: 'Complete the fixture.' }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Waiting for you.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Ask me to complete the page.');
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect(canvas).toBeVisible({ timeout: 30_000 });
		await expect.poll(() => canvas.evaluate(node => {
			const frame = node as HTMLCanvasElement;
			return Array.from(frame.getContext('2d')!.getImageData(5, 5, 1, 1).data).slice(0, 3);
		}), { timeout: 20_000 }).toEqual([240, 240, 240]);
		await expect(page.getByRole('button', { name: /Do: Your turn in the shared browser/ })).toBeVisible({ timeout: 15_000 });
		await saveEvidenceScreenshot(page, info, 'person-step-shared');
		const address = page.getByRole('textbox', { name: 'Browser address' });
		await address.fill(`${probe.origin}/browser-pane-probe.html?navigated`);
		await address.press('Enter');
		await expect(page.getByRole('button', { name: /Do: Your turn in the shared browser/ })).toHaveCount(0, { timeout: 20_000 });
	} finally { await probe.close(); }
});

test('browser_open shares a running headless browser', async ({ page, request }, info) => {
	test.skip(info.project.name === 'chromium-mobile', 'desktop proof');
	test.setTimeout(90_000);
	const probe = await startProbeServer();
	try {
		const title = `open-share-${Date.now()}`;
		const thread = await createSession(request, title);
		const url = `${probe.origin}/browser-pane-probe.html`;
		await loginByUrl(page);
		await sidebarThread(page, title).click();
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'headless-open', name: 'browser_open', arguments: JSON.stringify({ url }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Headless ready.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Open the fixture.');
		await expect(page.getByRole('button', { name: /Expand Worked/ })).toBeVisible({ timeout: 30_000 });
		await expect(page.locator('canvas[aria-label="Shared browser"]')).toHaveCount(0);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'shared-open', name: 'browser_open', arguments: JSON.stringify({ url, show_window: true }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Shared.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Share the fixture.');
		const canvas = page.locator('canvas[aria-label="Shared browser"]');
		await expect(canvas).toBeVisible({ timeout: 30_000 });
		await expect.poll(() => canvas.evaluate(node => {
			const frame = node as HTMLCanvasElement;
			return Array.from(frame.getContext('2d')!.getImageData(5, 5, 1, 1).data).slice(0, 3);
		}), { timeout: 20_000 }).toEqual([240, 240, 240]);
		await saveEvidenceScreenshot(page, info, 'open-shared');
	} finally { await probe.close(); }
});
