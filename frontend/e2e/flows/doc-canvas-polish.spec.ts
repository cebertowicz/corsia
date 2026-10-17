import path from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, sendChatMessage, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { deleteRenderedLine, openLiveDocument } from '../support/liveDocument';
import { readMockCallCount, seedMockResponses } from '../support/mock';

async function chooseThread(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click();
}
async function measurements(page: Page, testInfo: TestInfo, label: string) {
	const values = await page.locator('.doc').evaluate((root) => {
		const box = (selector: string) => {
			const rect = root.querySelector(selector)?.getBoundingClientRect();
			return rect ? { top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height } : null;
		};
		return {
			header: box('.document-header'), identity: box('.document-identity'), actions: box('.document-status-row'),
			editor: box('.doc-main'), drawer: box('[data-doc-comments-drawer]'), text: box('.cm-comment-focused'),
			avatarPositions: [...root.querySelectorAll('.presence-row .avatar')].map((element) => {
				const rect = element.getBoundingClientRect(); return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
			}),
			gap: Number.parseFloat(getComputedStyle(root.querySelector('.document-header')!).rowGap),
			viewportHeight: window.visualViewport?.height ?? window.innerHeight,
			visibleLines: [...root.querySelectorAll('.cm-line')].filter((line) => {
				const rect = line.getBoundingClientRect(), editor = root.querySelector('.doc-main')!.getBoundingClientRect();
				return rect.top >= editor.top && rect.bottom <= editor.bottom && line.textContent?.trim();
			}).map((line) => line.textContent),
		};
	});
	await testInfo.attach(`${label}-measurements`, { body: JSON.stringify(values, null, 2), contentType: 'application/json' });
	await saveEvidenceScreenshot(page, testInfo, label, { fullPage: false });
	return values;
}

test('@surface:doc-canvas-polish @flow:doc-canvas-polish pinned header, real arrival pill, and in-canvas comment navigation', async ({ page, request, browser }, testInfo) => {
	test.setTimeout(90_000);
	const mobile = testInfo.project.name.includes('mobile');
	if (mobile) await page.setViewportSize({ width: 393, height: 852 });
	else await page.setViewportSize({ width: 1440, height: 1000 });
	await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' });
	const stamp = Date.now(), viewerTitle = `canvas-viewer-${stamp}`, agentTitle = `Canvas Reviewer ${stamp}`, secondAgentTitle = `Canvas Reader ${stamp}`;
	// The viewer is not in their own presence row, so a second reader keeps three avatars in the stack.
	const thirdAgentTitle = `Canvas Skimmer ${stamp}`;
	const viewerId = await createSession(request, viewerTitle);
	const agentId = await createSession(request, agentTitle);
	const readerIds = [await createSession(request, thirdAgentTitle), await createSession(request, secondAgentTitle)];
	const token = encodeURIComponent(TEST_TOKEN);
	const tree = await request.get(`/api/files/tree?token=${token}`);
	expect(tree.ok()).toBeTruthy();
	const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `canvas-polish-${stamp}.md`, docPath = path.join(workspace, name);
	const quote = 'Canvas arrival target sentence.';
	const text = '# Canvas review\n\n' + Array.from({ length: 70 }, (_, index) => `Paragraph ${index + 1}: document context before the arrival.\n\n`).join('')
		+ quote + '\n\nSurrounding context after the selected text.\n\nMore surrounding context.\n';
	const seeded = await request.put(`/api/files/content?token=${token}`, { data: { path: docPath, text, thread_id: viewerId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	await page.addInitScript(({ viewerId, docPath, name, artifactId }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [viewerId]: {
			tabs: [{ instanceId: 'canvas-polish', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: 'canvas-polish', visible: true, activeSurface: 'artifact',
		} }, splitWidth: 540 }));
	}, { viewerId, docPath, name, artifactId });
	await loginByUrl(page);
	await chooseThread(page, viewerTitle);
	await expect(page.locator('.cm-content')).toContainText('Paragraph 1:');
	await expect(page.getByRole('button', { name: /^0 open threads\b/ })).toBeEnabled();
	if (!mobile) {
		await expect(page.locator('.leaf:not(.compact)')).toBeVisible();
		await measurements(page, testInfo, 'canvas-desktop-expanded');
		if (await page.locator('.pane').evaluate((element) => element.classList.contains('collapsed'))) {
			await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
		}
		await expect(page.locator('.pane:not(.collapsed)')).toBeVisible();
	}
	const header = page.locator('.document-header'), scroller = page.locator('.doc-main .cm-scroller');
	const pinnedTop = (await header.boundingBox())!.y;
	await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
	await expect.poll(async () => (await header.boundingBox())!.y).toBeCloseTo(pinnedTop, 0);
	await scroller.evaluate((element) => { element.scrollTop = 0; });
	await expect.poll(() => scroller.evaluate((element) => element.scrollTop)).toBe(0);

	// A real tool call creates the comment and emits doc_activity. No websocket frame is injected.
	const reader = await page.context().newPage();
	await loginByUrl(reader);
	// The mock's call count is shared by every thread, and earlier specs leave threads whose wakes arrive at any time (an idle timer,
	// a held message). Each seeded reply names the thread it serves so only this test's own calls are counted.
	for (const [index, title] of [thirdAgentTitle, secondAgentTitle].entries()) {
		await chooseThread(reader, title);
		await waitForWsConnected(reader);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: `canvas-read-${stamp}-${index}`, name: 'read_file', arguments: JSON.stringify({ path: docPath }) }], finish_reason: 'tool_calls', match_thread: readerIds[index] },
			{ content: 'Read complete.', finish_reason: 'stop', match_thread: readerIds[index] },
		]);
		await sendChatMessage(reader, 'Read this document.');
		await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(2);
	}
	await chooseThread(reader, agentTitle);
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `canvas-comment-${stamp}`, name: 'doc_comment_create', arguments: JSON.stringify({ artifact_id: artifactId, quote, body: 'Review the off-screen target.' }) }], finish_reason: 'tool_calls', match_thread: agentId },
		// The reviewer's post-tool reply and the owner's wake are each served to their own thread.
		{ content: 'Review recorded.', finish_reason: 'stop', match_thread: agentId },
		{ content: 'Review recorded.', finish_reason: 'stop', match_thread: viewerId },
	]);
	await sendChatMessage(reader, 'Comment on the target sentence.');
	await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(3);
	// The reviewer's comment is proven where it matters: the owner's canvas
	// below shows its arrival pill. Chat replies sit in Focus-mode groups whose
	// labels vary by viewport, so they are not asserted here.
	await reader.close();
	await expect(page.getByRole('group', { name: '3 participants', exact: true })).toBeVisible();
	const pill = page.getByRole('button', { name: '1 new below, go to nearest', exact: true });
	await expect(pill).toBeVisible();
	await pill.focus();
	await expect(pill).toBeFocused();
	await saveEvidenceScreenshot(page, testInfo, 'canvas-offscreen-pill', { fullPage: false });
	await pill.click();
	await expect(page.locator('.cm-comment-anchor').filter({ hasText: quote })).toBeInViewport();
	await expect.poll(() => page.locator('.doc-main').evaluate((element) => element.scrollTop)).toBe(0);
	await expect(page.locator('[data-doc-activity-pill]')).toHaveCount(0);
	await scroller.evaluate((element) => { element.scrollTop = 0; });
	const paneLabel = await page.locator('.pane-scope').textContent();
	await page.getByRole('button', { name: /^1 open thread\b/ }).click();
	const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
	await expect(drawer).toBeVisible();
	const viewportBefore = await page.evaluate(() => window.visualViewport?.height ?? window.innerHeight);
	await drawer.getByRole('button', { name: `Go to text: ${quote}`, exact: true }).click();
	await expect(page.locator('.cm-comment-focused').filter({ hasText: quote })).toBeInViewport();
	// The jump repeats until measured line heights settle; wait for the text to
	// rest inside the editor with the scroll position unchanged across a few
	// frames. Each value is the clearance in px (-Infinity while still moving),
	// so a miss reports how far off it landed.
	await expect.poll(() => page.locator('.doc').evaluate(async (root) => {
		const scroller = root.querySelector('.cm-scroller')!;
		const before = scroller.scrollTop;
		for (let frame = 0; frame < 3; frame += 1) await new Promise(requestAnimationFrame);
		if (scroller.scrollTop !== before) return -Infinity;
		const text = root.querySelector('.cm-comment-focused')!.getBoundingClientRect();
		const editor = root.querySelector('.doc-main')!.getBoundingClientRect();
		return Math.min(text.top - editor.top, editor.bottom - text.bottom);
	})).toBeGreaterThanOrEqual(0);
	if (mobile) {
		await expect(page.getByRole('region', { name: 'Document text', exact: true })).toBeFocused();
		expect(await page.evaluate(() => document.activeElement?.getAttribute('contenteditable'))).not.toBe('true');
		expect(await page.evaluate(() => window.visualViewport?.height ?? window.innerHeight)).toBe(viewportBefore);
	} else {
		await expect(page.locator('.cm-content')).toBeFocused();
		await expect(page.locator('.pane:not(.collapsed)')).toBeVisible();
		await expect(page.locator('.pane-scope')).toHaveText(paneLabel!);
		await expect(page.locator('.pane [data-doc-comments-drawer], .pane .dcp-head')).toHaveCount(0);
	}
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		const measured = await measurements(page, testInfo, `canvas-${theme}-normal`);
		expect(measured.text!.top).toBeGreaterThanOrEqual(measured.editor!.top);
		expect(measured.text!.bottom).toBeLessThanOrEqual(measured.editor!.bottom);
		expect(measured.editor!.height).toBeGreaterThan(measured.text!.height * 3);
		if (mobile) {
			expect(measured.header!.height).toBeCloseTo(measured.identity!.height + measured.actions!.height + measured.gap, 0);
			expect(measured.visibleLines.length).toBeGreaterThanOrEqual(3);
			// A commented line also carries its "Comment" marker button.
			expect(measured.visibleLines.some((line) => line?.includes(quote))).toBe(true);
			expect(measured.avatarPositions).toHaveLength(3);
			const [a, b, c] = measured.avatarPositions;
			expect(Math.abs((b.left - a.left) - (c.left - b.left))).toBeLessThanOrEqual(1);
		}
	}
	if (mobile) {
		// Emulate the shortened visual viewport while a reply owns focus.
		const reply = drawer.locator('.ct-input').first();
		await reply.focus();
		await page.setViewportSize({ width: 393, height: 520 });
		await expect(drawer).toBeVisible();
		await expect(reply).toBeFocused();
		await expect.poll(async () => (await drawer.boundingBox())!.y + (await drawer.boundingBox())!.height).toBeLessThanOrEqual(520);
		await expect(reply).toBeInViewport();
		await saveEvidenceScreenshot(page, testInfo, 'canvas-reply-short-viewport', { fullPage: false });
		await page.setViewportSize({ width: 393, height: 852 });
	}
	// This canvas is under 900 px, so the list is a modal sheet over the document column (RSR-7): Escape inside its controls
	// closes it and returns focus to the pill, and the document's selection Comment button works once it has closed.
	await drawer.getByRole('button', { name: 'Filter comments: Open', exact: true }).focus();
	await page.keyboard.press('Escape');
	await expect(drawer).toHaveCount(0);
	await expect(page.getByRole('button', { name: /^1 open thread\b/ })).toBeFocused();
	await page.locator('.cm-content').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await expect(page.getByRole('button', { name: 'Comment', exact: true })).toBeVisible();

	// Removing a known quote in the real editor produces the recovery row without hiding it in details.
	const edited = text.replace(quote, 'The selected text has been removed.');
	if (mobile) {
		// The Pixel profile puts CodeMirror on its Android input path, where one
		// synthetic multi-kilobyte insertText can reach the DOM but not the editor
		// state. Remove the quote from another live editor instead, the other way a
		// quote disappears: an open document refuses a plain file save (document_live).
		const writerContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
		try {
			const writer = await writerContext.newPage();
			await openLiveDocument(writer, { artifactId, body: text, docPath, fileName: name, threadId: viewerId, title: viewerTitle }, '# Canvas review');
			// CodeMirror renders only lines near the viewport and the quote sits at the end. Scroll the scroller itself:
			// a click plus ControlOrMeta+End only moves the caret to the end of the first line on macOS.
			await writer.evaluate(() => { const scroller = document.querySelector('.cm-scroller')!; scroller.scrollTop = scroller.scrollHeight; });
			await expect(writer.locator('.cm-content')).toContainText(quote);
			await deleteRenderedLine(writer, quote);
			await expect(page.locator('.cm-content')).not.toContainText(quote);
		} finally {
			await writerContext.close();
		}
	} else {
		await page.locator('.cm-content').click();
		await expect(page.locator('.cm-content')).toBeFocused();
		await page.keyboard.press('ControlOrMeta+A');
		await page.keyboard.insertText(edited);
	}
	await expect(page.locator('.cm-content')).not.toContainText(quote);
	await expect(page.locator('.comment-status:has(.unplaced-dot)')).toBeVisible();
	if (mobile) {
		// At phone width the control is 44px tall; the dot must still sit on the comment icon, and the 12px padding must apply.
		const fit = await page.locator('.comment-status').evaluate((button) => {
			const icon = button.querySelector('.tool-icon')!.getBoundingClientRect();
			const dot = button.querySelector('.unplaced-dot')!.getBoundingClientRect();
			return { dx: Math.abs(dot.left + dot.width / 2 - (icon.right)), dy: Math.abs(dot.top + dot.height / 2 - icon.top), padding: getComputedStyle(button).paddingLeft };
		});
		expect(fit.dx).toBeLessThanOrEqual(8);
		expect(fit.dy).toBeLessThanOrEqual(8);
		expect(fit.padding).toBe('12px');
	}
	await page.locator('.comment-status:has(.unplaced-dot)').click();
	await expect(drawer).toBeVisible();
	await drawer.getByRole('button', { name: 'Show comment: text unavailable', exact: true }).click();
	await expect(drawer).toContainText('This comment has no text to jump to.');
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		await measurements(page, testInfo, `canvas-${theme}-recovery`);
		// The tools sit in the identity row; at phone width each is 44px tall (DocViewerStatus.svelte, `.doc-tool` min-height).
		if (mobile) expect((await page.locator('.comment-status').boundingBox())!.height).toBeGreaterThanOrEqual(44);
	}
});
