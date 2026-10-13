import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { cueSamples, expectQuietSave, observeSaveCue, routeDocumentSaves } from '../support/docSaveCue';
import { openLiveDocument, seedLiveDocument, serverText, typeAtDocumentEnd } from '../support/liveDocument';

// Preserve full-screen motion evidence for review, including successful runs.
test.use({ video: 'on' });

async function evidence(page: Page, info: TestInfo, name: string) {
	await info.attach(`${name}-frames`, { body: JSON.stringify(await cueSamples(page), null, 2), contentType: 'application/json' });
	await page.screenshot({ path: info.outputPath(`${name}.png`), fullPage: true });
}
async function assertStamp(page: Page) {
	if (process.env.CORSIA_E2E_EXPECTED_HEAD) {
		await expect(page.locator('.version-overlay')).toContainText(process.env.CORSIA_E2E_EXPECTED_HEAD.slice(0, 7));
	}
}

for (const width of [1440, 393]) for (const theme of ['light', 'dark'] as const) {
	test(`quiet save slow typing ${width} ${theme}`, async ({ page, request, context }, info) => {
		await page.setViewportSize({ width, height: width === 1440 ? 960 : 852 });
		await page.addInitScript(value => localStorage.setItem('corsia_theme', value), theme);
		const link = await routeDocumentSaves(context, 1500);
		const fixture = await seedLiveDocument(request, `quiet-slow-${width}-${theme}`, '# Writing with pauses\n\nA slow connection should explain itself.\n');
		await openLiveDocument(page, fixture);
		await assertStamp(page);
		if (width === 1440) await expect(page.locator('.chat-col')).toBeVisible();
		await observeSaveCue(page);
		await typeAtDocumentEnd(page, ' First thought.');
		await expect.poll(() => link.starts).toBe(1);
		await expect(page.locator('.save-ghost')).toHaveCount(0);
		await expect(page.getByText('Saving…', { exact: true })).toBeVisible();
		await page.keyboard.type(' And a second thought.');
		await expect(page.getByText('Saving…', { exact: true })).toBeVisible();
		await expect.poll(() => link.starts).toBeGreaterThanOrEqual(2);
		await expect(page.getByText('Saving…', { exact: true })).toBeVisible();
		await expect(page.getByText('✓ Saved', { exact: true })).toBeVisible();
		await expect(page.locator('.save-ghost')).toHaveCount(0);
		await expect.poll(() => serverText(request, fixture)).toContain('And a second thought.');
		const frames = await cueSamples(page);
		const firstSaving = frames.findIndex(frame => frame.cue === 'Saving…' && frame.visible);
		const firstSaved = frames.findIndex(frame => frame.cue === '✓ Saved' && frame.visible);
		expect(firstSaving).toBeGreaterThan(0); expect(firstSaved).toBeGreaterThan(firstSaving);
		expect(frames.slice(firstSaving, firstSaved).every(frame => frame.cue === 'Saving…' && frame.visible)).toBe(true);
		await evidence(page, info, `slow-${width}-${theme}`);

	});
	test(`quiet save reconnect ${width} ${theme}`, async ({ page, request, context }, info) => {
		await page.setViewportSize({ width, height: width === 1440 ? 960 : 852 });
		await page.addInitScript(value => localStorage.setItem('corsia_theme', value), theme);
		const link = await routeDocumentSaves(context, 30);
		const fixture = await seedLiveDocument(request, `quiet-reconnect-${width}-${theme}`, '# Reconnect confirmation\n\nWriting stays here while the connection returns.\n');
		await openLiveDocument(page, fixture); await assertStamp(page);
		if (width === 1440) await expect(page.locator('.chat-col')).toBeVisible();
		await observeSaveCue(page);
		await link.cut();
		await typeAtDocumentEnd(page, ' An offline thought.');
		await expect(page.getByText(/Disconnected/).first()).toBeVisible();
		await expect.poll(async () => {
			const source = '/src/lib/stores/docSaveQueue.ts';
			return page.evaluate(async source => {
				const { docSaveActivities } = await import(/* @vite-ignore */ source);
				let owed = false;
				const stop = docSaveActivities.subscribe((all: any) => { owed = all['live-tab']?.confirmationOwed ?? false; });
				stop();
				return owed;
			}, source);
		}).toBe(true);
		await expect(page.locator('.save-ghost')).toHaveCount(0);
		link.restore();
		await expect(page.getByText('✓ Saved', { exact: true })).toBeVisible({ timeout: 20_000 });
		await expect.poll(() => serverText(request, fixture)).toContain('An offline thought.');
		await expect(page.locator('.save-ghost')).toHaveCount(0);
		const frames = await cueSamples(page);
		expect(frames.filter(frame => frame.disconnected && frame.visible)).toEqual([]);
		expect(frames.some(frame => frame.cue === '✓ Saved' && frame.visible)).toBe(true);
		await evidence(page, info, `reconnect-${width}-${theme}`);
	});
}

test('quiet save narrow Reload precedence 393 component layout', async ({ page, request }, info) => {
	await page.setViewportSize({ width: 393, height: 852 });
	const fixture = await seedLiveDocument(request, 'quiet-reload-layout', '# Reload takes precedence\n\nSnapshot header layout fixture.\n');
	await openLiveDocument(page, fixture); await assertStamp(page);
	// Live sessions intentionally suppress Reload. Mount the snapshot status component
	// inside the real canvas: its container-query precedence needs browser layout, not jsdom.
	await page.evaluate(async () => {
		const svelte = '/node_modules/.vite/deps/svelte.js';
		const component = '/src/lib/components/DocViewerStatus.svelte';
		const { mount } = await import(/* @vite-ignore */ svelte);
		const { default: Status } = await import(/* @vite-ignore */ component);
		const original = document.querySelector<HTMLElement>('.document-header')!;
		original.style.display = 'none';
		const target = document.createElement('div'); original.after(target);
		mount(Status, { target, props: { ready: true, stale: true, readonly: false,
			saveStatus: 'saving', commentCount: 1, orphanedCount: 0, artifactId: null,
			targetInstanceId: null, freshness: null, onOpenComments: () => {} } });
	});
	await expect(page.getByRole('button', { name: 'File changed · Reload' })).toBeVisible();
	await expect(page.getByText('Saving…', { exact: true })).toHaveCount(1);
	await expect(page.getByText('Saving…', { exact: true })).not.toBeVisible();
	await evidence(page, info, 'narrow-reload-component-393');
});

test('quiet save ordinary fast pause 1440', async ({ page, request, context }, info) => {
	await page.setViewportSize({ width: 1440, height: 960 });
	await routeDocumentSaves(context);
	const fixture = await seedLiveDocument(request, 'quiet-fast', '# A quiet writing pause\n\nThe header stays still.\n');
	await openLiveDocument(page, fixture); await assertStamp(page);
	await observeSaveCue(page);
	await typeAtDocumentEnd(page, ' A quick addition.');
	await expect.poll(() => serverText(request, fixture)).toContain('A quick addition.');
	await expectQuietSave(page);
	await evidence(page, info, 'fast-1440');
});
