
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, saveEvidenceScreenshot, waitForWsConnected, sidebarThread } from '../support/corsia';
import { focusSetting } from '../support/focusOutcomeProof';
import { setFocusMode } from '../support/focusMode';

// OlderHistoryRow.svelte: the explicit way out when scrolling cannot page, shown while older history remains.
const olderRow = (page: Page) => page.getByRole('button', { name: 'Load older messages' });

function makeMsg(id: string, sessionId: string, role: 'user' | 'assistant', content: string, ts: string) {
	return { id, thread_id: sessionId, role, content, created_at: ts, metadata: null };
}

test.describe('@flow:paging @surface:chat conversation message paging', () => {
	// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('long conversation stays bounded and scrolls with wheel input', async ({ page, request }) => {
		const sessionTitle = `e2e convo-scroll-container ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);
		const rows = Array.from({ length: 30 }, (_, i) =>
			makeMsg(
				`msg-scroll-${String(i).padStart(2, '0')}`,
				sessionId,
				i % 2 === 0 ? 'user' : 'assistant',
				`Scrollable message ${i}: ${'history detail '.repeat(24)}`,
				`2026-06-12T01:00:00.${String(i).padStart(6, '0')}`,
			),
		);

		await page.route(`**/api/threads/${sessionId}/messages**`, async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ messages: rows, has_more: false, oldest_id: null }),
			});
		});

		await openAppPastLanding(page);
		// Focus is on by default since #565 and hides finished work; this spec counts raw rows.
		await focusSetting(page, false);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-title', { hasText: sessionTitle }).click({ timeout: 10_000 });
		await waitForWsConnected(page);
		await expect(page.locator('.message')).toHaveCount(rows.length);

		const timeline = page.locator('.messages');
		const bottom = await timeline.evaluate((el) => {
			el.scrollTop = el.scrollHeight;
			return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
		});
		expect(bottom.scrollHeight).toBeGreaterThan(bottom.clientHeight);
		expect(bottom.scrollTop).toBeGreaterThan(0);

		await timeline.hover();
		await page.mouse.wheel(0, -600);
		await expect.poll(() => timeline.evaluate((el) => el.scrollTop)).toBeLessThan(bottom.scrollTop);
	});

	test('scroll-up triggers paging and prepends older messages', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);

		const sessionTitle = `e2e convo-paging-scroll ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);

		const newMsgs = Array.from({ length: 10 }, (_, i) =>
			makeMsg(
				`msg-new-${String(i).padStart(2, '0')}`,
				sessionId,
				i % 2 === 0 ? 'user' : 'assistant',
				`Recent message ${i}`,
				`2026-06-12T01:00:00.${String(i).padStart(6, '0')}`,
			),
		);
		const oldMsgs = Array.from({ length: 5 }, (_, i) =>
			makeMsg(
				`msg-old-${String(i).padStart(2, '0')}`,
				sessionId,
				i % 2 === 0 ? 'user' : 'assistant',
				`Older message ${i}`,
				`2026-06-12T00:00:00.${String(i).padStart(6, '0')}`,
			),
		);

		let releaseOlderFetch!: () => void;
		const olderFetchGate = new Promise<void>((resolve) => {
			releaseOlderFetch = resolve;
		});

		await page.route(`**/api/threads/${sessionId}/messages**`, async (route) => {
			const url = route.request().url();
			if (url.includes('before=')) {
				await olderFetchGate;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ messages: oldMsgs, has_more: false, oldest_id: null }),
				});
			} else {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						messages: newMsgs,
						has_more: true,
						oldest_id: newMsgs[0].id,
					}),
				});
			}
		});

		await openAppPastLanding(page);
		// Focus is on by default since #565 and hides finished work; this spec counts raw rows.
		await focusSetting(page, false);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
		await waitForWsConnected(page);

		// MessageList.svelte pages on its own when the view cannot scroll (10 short rows), so the held fetch is already
		// in flight here; OlderHistoryRow.svelte shows its button only while no fetch is pending.
		await expect(page.locator('.loading-older svg.spin')).toBeVisible({ timeout: 10_000 });
		const initialCount = await page.locator('.message').count();
		expect(initialCount).toBe(10);
		releaseOlderFetch(); // let the mock respond now

		await expect(page.locator('.loading-older svg.spin')).not.toBeVisible({ timeout: 10_000 });
		const finalCount = await page.locator('.message').count();
		expect(finalCount).toBe(15); // 10 recent + 5 older

		await expect(olderRow(page)).not.toBeVisible({ timeout: 5_000 });

		expect(consoleErrors).toEqual([]);
		await saveEvidenceScreenshot(page, testInfo, 'paging-scroll-up-complete');
	});

	test('failed older-page fetch shows retry pill; retry recovers', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);

		const sessionTitle = `e2e convo-paging-retry ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);

		const newMsgs = Array.from({ length: 10 }, (_, i) =>
			makeMsg(
				`msg-new-${String(i).padStart(2, '0')}`,
				sessionId,
				i % 2 === 0 ? 'user' : 'assistant',
				`Message ${i}`,
				`2026-06-12T01:00:00.${String(i).padStart(6, '0')}`,
			),
		);
		const oldMsgs = [
			makeMsg('msg-old-00', sessionId, 'user', 'Recovered older message', '2026-06-12T00:00:00.000000'),
		];

		let olderCallCount = 0;
		await page.route(`**/api/threads/${sessionId}/messages**`, async (route) => {
			const url = route.request().url();
			if (url.includes('before=')) {
				olderCallCount++;
				if (olderCallCount === 1) {
					await route.fulfill({ status: 500, body: 'internal server error' });
				} else {
					await route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({ messages: oldMsgs, has_more: false, oldest_id: null }),
					});
				}
			} else {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						messages: newMsgs,
						has_more: true,
						oldest_id: newMsgs[0].id,
					}),
				});
			}
		});

		await openAppPastLanding(page);
		// Focus is on by default since #565 and hides finished work; this spec counts raw rows.
		await focusSetting(page, false);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
		await waitForWsConnected(page);

		// The view cannot scroll, so MessageList.svelte pages on its own and the first (failing) fetch runs unprompted.
		await expect(page.getByRole('button', { name: 'Retry loading older messages' })).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.loading-older svg.spin')).not.toBeVisible();

		await page.getByRole('button', { name: 'Retry loading older messages' }).click();
		await expect(page.getByRole('button', { name: 'Retry loading older messages' })).not.toBeVisible({ timeout: 10_000 });
		await expect(page.getByText('Recovered older message')).toBeVisible({ timeout: 10_000 });
		const finalCount = await page.locator('.message').count();
		expect(finalCount).toBe(11); // 10 + 1 recovered

		expect(olderCallCount).toBe(2);

		const unexpectedErrors = consoleErrors.filter(
			(e) => !e.includes('Failed to load resource'),
		);
		expect(unexpectedErrors).toEqual([]);
		await saveEvidenceScreenshot(page, testInfo, 'paging-retry-recovered');
	});

	test('visibility refetch keeps an overlapping older page unique and ordered', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const sessionTitle = `e2e convo-paging-visibility-overlap ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);
		const rows = (start: number, end: number) => Array.from({ length: end - start + 1 }, (_, offset) => {
			const index = start + offset;
			return makeMsg(
				`message-${String(index).padStart(3, '0')}`,
				sessionId,
				index % 2 === 0 ? 'user' : 'assistant',
				`Transcript message ${index}`,
				new Date(Date.UTC(2026, 6, 27, 0, index)).toISOString(),
			);
		});
		const newest = rows(51, 100);
		const older = rows(1, 50);
		let firstPageFetches = 0;
		let olderFetches = 0;
		await page.route(`**/api/threads/${sessionId}/messages**`, async (route) => {
			if (route.request().url().includes('before=')) {
				olderFetches += 1;
				await route.fulfill({
					contentType: 'application/json',
					body: JSON.stringify({ messages: older, has_more: false, oldest_id: 'message-001' }),
				});
				return;
			}
			firstPageFetches += 1;
			await route.fulfill({
				contentType: 'application/json',
				body: JSON.stringify({ messages: newest, has_more: true, oldest_id: 'message-051' }),
			});
		});

		await openAppPastLanding(page);
		// Focus is on by default since #565 and hides finished work; this spec counts raw rows.
		await focusSetting(page, false);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
		await waitForWsConnected(page);
		await expect(page.locator('.message')).toHaveCount(50);

		const messages = page.locator('.messages');
		// A wheel gesture is what a person does: it ends any programmatic pin to the bottom that still ignores scroll events.
		await messages.evaluate((el) => { el.scrollTop = 0; });
		await messages.hover();
		await page.mouse.wheel(0, -600);
		await expect.poll(() => olderFetches).toBe(1);
		await expect(page.locator('.message')).toHaveCount(100);

		// Headless tab focus does not guarantee a visibility transition.
		await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
		expect(await page.evaluate(() => document.visibilityState)).toBe('visible');
		await expect.poll(() => firstPageFetches).toBe(2);

		// A wheel gesture is what a person does: it ends any programmatic pin to the bottom that still ignores scroll events.
		await messages.evaluate((el) => { el.scrollTop = 0; });
		await messages.hover();
		await page.mouse.wheel(0, -600);
		await expect.poll(() => olderFetches).toBe(2);
		await expect(page.locator('.message')).toHaveCount(100);
		await expect(page.locator('.message').first()).toContainText('Transcript message 1');
		await expect(page.locator('.message').last()).toContainText('Transcript message 100');
		expect(await page.locator('.message').evaluateAll((items) => new Set(items.map((item) => item.textContent)).size)).toBe(100);
		expect(consoleErrors).toEqual([]);
		await saveEvidenceScreenshot(page, testInfo, 'paging-visibility-overlap');
	});
});
