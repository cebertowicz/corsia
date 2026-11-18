import { test } from '../support/test';
import { expect } from '@playwright/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { expectCommentsShown, openComments, showAllThreads } from '../support/commentDrawer';
import { loginByUrl } from '../support/corsia';

test('@probe:overlap-live accepting while typing in a live document', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop only');
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const opened = new Set<string>();
	page.on('websocket', socket => socket.on('framereceived', frame => {
		try {
			const msg = JSON.parse(String(frame.payload));
			if (msg.type === 'doc_opened') opened.add(msg.artifact_id);
		} catch { /* Ignore non-JSON frames. */ }
	}));
	const initial = '# Overlap\n\nThe original sentence is here.\n';
	const doc = await openSuggestionDocument(page, request, initial, [
		{ quote: 'original sentence', replacement: 'accepted sentence', body: 'Apply this change.' },
	]);
	await expect.poll(() => opened.has(doc.artifactId), { timeout: 15_000 }).toBe(true);
	const second = await page.context().newPage();
	const openedSecond = new Set<string>();
	second.on('websocket', socket => socket.on('framereceived', frame => {
		try {
			const msg = JSON.parse(String(frame.payload));
			if (msg.type === 'doc_opened') openedSecond.add(msg.artifact_id);
		} catch { /* Ignore non-JSON frames. */ }
	}));
	await loginByUrl(second);
	await second.locator('.thread-item').filter({ hasText: 'suggestions-owner-' }).click();
	const secondEditor = second.locator('.cm-content');
	await expect(secondEditor).toContainText('original sentence');
	await expect.poll(() => openedSecond.has(doc.artifactId), { timeout: 15_000 }).toBe(true);
	await expect(secondEditor).not.toHaveAttribute('aria-readonly', 'true');
	await openComments(page, /^1 comment\b/);
	await expectCommentsShown(doc.drawer);
	await showAllThreads(doc.drawer);

	let release!: () => void;
	let intercepted!: () => void;
	const waiting = new Promise<void>(resolve => { intercepted = resolve; });
	const proceed = new Promise<void>(resolve => { release = resolve; });
	await page.route('**/api/comments/*/decision?*', async route => {
		intercepted();
		await proceed;
		await route.continue();
	});
	await doc.accept(doc.ids[0]).click();
	await waiting;
	await secondEditor.click();
	await second.keyboard.press('ControlOrMeta+End');
	await second.keyboard.type('\nTyped during accept.');
	await expect(secondEditor).toContainText('Typed during accept.');
	const response = page.waitForResponse(r => r.url().includes(`/api/comments/${doc.ids[0]}/decision`));
	release();
	expect((await response).status()).toBe(200);
	await expect(doc.editor).toContainText('accepted sentence');
	await expect(doc.editor).toContainText('Typed during accept.');
	await expect.poll(doc.storedText, { timeout: 20_000 }).toContain('accepted sentence');
	await expect.poll(doc.storedText, { timeout: 20_000 }).toContain('Typed during accept.');
	await page.screenshot({ path: testInfo.outputPath('live-overlap.png') });
});
