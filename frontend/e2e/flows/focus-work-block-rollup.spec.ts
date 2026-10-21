import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { createSession, openAppByUrl, TEST_TOKEN, waitForWsConnected, watchSessionSubscriptions } from '../support/corsia';
import { injectMessage } from '../support/mock';

type LiveState =
	| 'divider'
	| 'divider-truncated'
	| 'parent-tool'
	| 'named-subagent'
	| 'unnamed-subagent'
	| 'newest-subagent'
	| 'parent-ended-child-running';

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByText(title, { exact: true }).first().click();
}

async function setLiveState(page: Page, parentId: string, state: LiveState): Promise<void> {
	await page.evaluate(async ({ parentId, state }) => {
		const [settings, threads, turns, subagents, updates, wsState] = await Promise.all([
			import('/src/lib/stores/settings.ts'),
			import('/src/lib/stores/threads.ts'),
			import('/src/lib/ws/turnState.ts'),
			import('/src/lib/stores/subagents.ts'),
			import('/src/lib/stores/threadUpdateCards.ts'),
			import('/src/lib/ws/state.ts'),
		]);
		// These frame fixtures own their synthetic descendants. Avoid a server snapshot
		// of a thread that deliberately does not exist in the disposable backend.
		wsState.setWs({ readyState: WebSocket.OPEN, send: () => {} } as any);
		const now = Date.now();
		settings.coreSettings.set({ features: { focus_mode: true } } as any);
		turns._resetTurnStateForTests();
		threads.activeToolsByThread.set(new Map());
		subagents.subagentMeta.set(new Map());
		updates.visitDividers.set({});
		const parent = { turnId: 'parent-turn', turnOrder: 1 };
		const child = (id: string, order: number, startedAt: number) =>
			turns.applyLifecycle(id, { turnId: `${id}-turn`, turnOrder: order }, parentId, startedAt);
		if (state === 'divider' || state === 'divider-truncated' || state === 'parent-tool') {
			// No stored Turn summary proves these starts, so the block shows no time (A9-A12).
			turns.applyLifecycle(parentId, parent, undefined, now - 125_000);
		}
		if (state === 'divider' || state === 'divider-truncated') {
			updates.visitDividers.set({
				[parentId]: {
					divider: {
						since: '2026-10-20T17:00:00Z', first_seq: 2,
						...(state === 'divider-truncated' ? { truncated: true as const } : {}),
					},
					failed: false,
				},
			});
			return;
		}
		if (state === 'parent-tool') {
			threads.activeToolsByThread.set(new Map([[parentId, [{ id: 'parent-tool', name: 'read_file', phrase: 'Reading parent source', status: 'running' }]]]) as any);
			return;
		}
		if (state === 'named-subagent' || state === 'unnamed-subagent') {
			child('child-one', 2, now - 185_000);
			threads.activeToolsByThread.set(new Map([['child-one', [{ id: 'child-one-tool', name: 'read_file', phrase: 'Inspecting component', status: 'running' }]]]) as any);
			if (state === 'named-subagent') subagents.registerSubagent(parentId, 'child-one', 'UI worker');
			return;
		}
		if (state === 'newest-subagent') {
			child('child-old', 2, now - 185_000);
			child('child-new', 3, now - 125_000);
			subagents.registerSubagent(parentId, 'child-old', 'Research worker');
			subagents.registerSubagent(parentId, 'child-new', 'Implementation worker');
			threads.activeToolsByThread.set(new Map([
				['child-old', [{ id: 'child-old-tool', name: 'read_file', phrase: 'Reading old task', status: 'running' }]],
				['child-new', [{ id: 'child-new-tool', name: 'write_file', phrase: 'Writing newest task', status: 'running' }]],
			]) as any);
			return;
		}
		child('child-after-parent', 2, now - 185_000);
		subagents.registerSubagent(parentId, 'child-after-parent', 'Follow-up worker');
		threads.activeToolsByThread.set(new Map([['child-after-parent', [{ id: 'child-after-parent-tool', name: 'read_file', phrase: 'Finishing follow-up', status: 'running' }]]]) as any);
	}, { parentId, state });
}

async function capture(page: Page, testInfo: TestInfo, name: string): Promise<void> {
	const root = process.env.FOCUS_WORK_BLOCK_PROOF_DIR ?? testInfo.outputPath('focus-work-block-rollup');
	mkdirSync(root, { recursive: true });
	await page.screenshot({ path: path.join(root, `${name}.png`), fullPage: false, animations: 'disabled' });
}

async function captureAtBreakpoints(
	page: Page,
	testInfo: TestInfo,
	theme: 'light' | 'dark',
	name: string,
): Promise<void> {
	for (const [label, size] of [
		['1440', { width: 1440, height: 900 }],
		['393', { width: 393, height: 852 }],
	] as const) {
		await page.setViewportSize(size);
		await capture(page, testInfo, `${name}-${label}-${theme}`);
	}
}

for (const theme of ['light', 'dark'] as const) {
	test(`@surface:focus-work-block-rollup Focus work-block states in ${theme}`, async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		const title = `focus-work-block-rollup-${theme}-${Date.now()}`;
		const parentId = await createSession(request, title);
		await injectMessage(request, parentId, {
			role: 'assistant', content: 'Background preparation.', metadata: { turn_id: 'parent-turn', tool_calls: [{ id: 'background-one', name: 'read_file', arguments: {} }] },
		}, TEST_TOKEN);
		await injectMessage(request, parentId, {
			role: 'assistant', content: 'Background follow-up.', metadata: { turn_id: 'parent-turn', tool_calls: [{ id: 'background-two', name: 'read_file', arguments: {} }] },
		}, TEST_TOKEN);
		const subscriptions = watchSessionSubscriptions(page);
		await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
		await page.setViewportSize({ width: 1440, height: 900 });
		await openThread(page, title);
		await waitForWsConnected(page);
		await subscriptions.waitForSubscribed(parentId);
		await expect(page.getByText("What's on your mind?")).toBeVisible();

		for (const state of ['divider', 'divider-truncated'] as const) {
			await setLiveState(page, parentId, state);
			const block = page.locator('.live-work');
			const divider = block.locator('[data-visit-divider]');
			await expect(block.locator('.fold-toggle')).toHaveAttribute('aria-label', /^Expand current work: Working$/);
			await expect(divider).toBeVisible();
			await expect(block.locator('.live-step')).toHaveText(/^Working$/);
			await captureAtBreakpoints(page, testInfo, theme, `${state}-collapsed`);
			await block.locator('.fold-toggle').click();
			await expect(block.locator('.fold-details [data-visit-divider]')).toBeVisible();
			await captureAtBreakpoints(page, testInfo, theme, `${state}-expanded`);
			await block.locator('.fold-toggle').click();
			await expect(block.locator('.fold-details')).toHaveCount(0);
		}

		await setLiveState(page, parentId, 'parent-tool');
		await expect(page.locator('.live-work .live-spinner')).not.toHaveClass(/subagent-only/);
		await expect(page.locator('.live-work .fold-toggle')).toHaveAttribute('aria-label', /^Expand current work: Reading parent source$/);
		await captureAtBreakpoints(page, testInfo, theme, 'parent-tool');

		await setLiveState(page, parentId, 'named-subagent');
		await expect.poll(() => page.evaluate(async () => {
			const threads = await import('/src/lib/stores/threads.ts');
			let phrase: string | null = null;
			const unsubscribe = threads.activeToolsByThread.subscribe((tools) => { phrase = tools.get('child-one')?.[0]?.phrase ?? null; });
			unsubscribe();
			return phrase;
		})).toBe('Inspecting component');
		await expect(page.locator('.live-work .live-spinner')).toHaveClass(/subagent-only/);
		await expect(page.locator('.live-work .live-step')).toHaveText(/^Working · UI worker: Inspecting component$/);
		await expect(page.locator('.live-work .fold-toggle')).toHaveAttribute('aria-label', /^Expand current work: UI worker: Inspecting component$/);
		await captureAtBreakpoints(page, testInfo, theme, 'named-subagent');

		await setLiveState(page, parentId, 'unnamed-subagent');
		await expect(page.locator('.live-work .live-spinner')).toHaveClass(/subagent-only/);
		await expect(page.locator('.live-work .live-step')).toHaveText(/^Working$/);
		await expect(page.locator('.live-work .fold-toggle')).toHaveAttribute('aria-label', /^Expand current work: Working$/);
		await captureAtBreakpoints(page, testInfo, theme, 'unnamed-subagent');

		await setLiveState(page, parentId, 'newest-subagent');
		await expect(page.locator('.live-work .live-step')).toHaveText(/^Working · Implementation worker: Writing newest task$/);
		await captureAtBreakpoints(page, testInfo, theme, 'newest-subagent');

		await setLiveState(page, parentId, 'parent-ended-child-running');
		await expect(page.locator('.live-work .live-step')).toHaveText(/^Working · Follow-up worker: Finishing follow-up$/);
		await captureAtBreakpoints(page, testInfo, theme, 'parent-ended-child-running');
	});
}
