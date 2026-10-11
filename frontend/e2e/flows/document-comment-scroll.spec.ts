import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN, watchSessionSubscriptions } from '../support/corsia';

test('@flow:document-comment-save preserves the visible editor viewport', async ({ page, request }, testInfo) => {
	if (testInfo.project.name === 'chromium-mobile') await page.setViewportSize({ width: 393, height: 852 });
	const title = `comment-scroll-${Date.now()}`;
	const threadId = await createSession(request, title);
	const token = TEST_TOKEN;
	const rootsResponse = await request.get(`/api/files/tree?token=${encodeURIComponent(token)}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const target = 'Viewport anchor sentence must remain where the reader left it.';
	const sections = Array.from({ length: 72 }, (_, index) => `## Section ${index}\n\nA public-safe paragraph that makes this document long enough to scroll in the editor.\n`);
	const text = [
		'# Public comment viewport fixture',
		'',
		'| Requirement | Status |',
		'| --- | --- |',
		'| Preserve the reader position | Required |',
		'',
		'```mermaid',
		'flowchart LR',
		'  Reader --> Comment',
		'```',
		'',
		...sections.slice(0, 36),
		target,
		'',
		...sections.slice(36),
	].join('\n');
	const docPath = path.join(workspace!, `${title}.md`);
	const seeded = await request.put(`/api/files/content?token=${encodeURIComponent(token)}`, {
		data: { path: docPath, text, thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	// This starts with no comments so the interaction exercises first-comment save.
	await page.addInitScript(({ threadId, docPath, title }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'scroll-tab', artifactId: null, path: docPath, title, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'scroll-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
		}));
	}, { threadId, docPath, title });
	const subscriptions = watchSessionSubscriptions(page);
	// Do not let the generic login helper select a new empty thread: this flow
	// must open the synthetic document's owning thread through the normal list.
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByText(title, { exact: true }).first().click();
	await subscriptions.waitForSubscribed(threadId);
	const stored = await request.get(`/api/files/content?token=${encodeURIComponent(token)}&path=${encodeURIComponent(docPath)}`);
	expect(stored.ok(), await stored.text()).toBeTruthy();
	expect((await stored.json()).text).toBe(text);
	const scroller = page.locator('.cm-scroller');
	await expect(scroller).toBeVisible();
	await expect(scroller).toContainText('Public comment viewport fixture');
	// The editor grows to its content height inside the document pane, so the
	// reader's viewport is the nearest scrolling ancestor, not `.cm-scroller`.
	const viewport = (await scroller.evaluateHandle((node) => {
		for (let element: HTMLElement | null = node as HTMLElement; element; element = element.parentElement) {
			const { overflowY } = getComputedStyle(element);
			if ((overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight) return element;
		}
		throw new Error('the document has no scrolling viewport');
	})).asElement()!;
	// CodeMirror renders only the lines near the visible viewport, so the target
	// is not in the DOM until the reader scrolls down to it.
	const targetLine = scroller.locator('.cm-line', { hasText: target });
	await expect.poll(async () => {
		if (await targetLine.count()) return true;
		await viewport.evaluate((element) => element.scrollBy(0, element.clientHeight / 2));
		return false;
	}, { message: 'target line never rendered while scrolling the document viewport', timeout: 15_000 }).toBe(true);
	// Leave the line in the upper part of the viewport rather than where
	// Playwright's scrolling centers it, so a save-time recenter is visible.
	await targetLine.scrollIntoViewIfNeeded();
	await expect(targetLine).toBeVisible();
	const offset = (await targetLine.boundingBox())!.y - (await viewport.boundingBox())!.y - 120;
	await viewport.evaluate((element, offset) => element.scrollBy(0, offset), offset);
	await expect.poll(async () => Math.abs((await targetLine.boundingBox())!.y - (await viewport.boundingBox())!.y - 120) <= 2).toBe(true);
	expect(await viewport.evaluate((element) => element.scrollTop), 'the reader viewport must be genuinely scrolled').toBeGreaterThan(0);
	await targetLine.click();
	await expect.poll(() => page.evaluate(() => {
		const node = document.getSelection()?.anchorNode;
		const element = node instanceof Element ? node : node?.parentElement;
		return element?.closest('.cm-line')?.textContent ?? null;
	}), { message: 'caret must land on the clicked line' }).toBe(target);
	const readerPosition = async () => ({
		page: await page.evaluate(() => window.scrollY),
		viewport: await viewport.evaluate((element) => element.scrollTop),
		viewportBox: await viewport.boundingBox(),
		contentHeight: await viewport.evaluate((element) => element.scrollHeight),
		editorTop: (await scroller.boundingBox())?.y ?? null,
		lineBox: await targetLine.boundingBox(),
		line: (await targetLine.boundingBox())?.y ?? null,
	});
	// Select the line's first word, as a reader commenting on "Viewport" would.
	await targetLine.dblclick({ position: { x: 12, y: 8 } });
	const before = await readerPosition();
	await page.locator('.sel-comment').click();
	await page.getByPlaceholder('Comment…').fill('Preserve this viewport.');
	const composerOpen = await readerPosition();
	// The first attempt fails; the reader keeps their draft and place, then retries.
	const commentsUrl = (url: URL) => /^\/api\/artifacts\/[^/]+\/comments$/.test(url.pathname);
	await page.route(commentsUrl, (route) => route.request().method() === 'POST'
		? route.fulfill({ status: 500, body: 'synthetic comment failure' })
		: route.fallback());
	await page.getByRole('button', { name: 'Comment', exact: true }).last().click();
	await expect(page.getByText("Couldn't post the comment — try again")).toBeVisible();
	await page.unroute(commentsUrl);
	const failed = await readerPosition();
	expect(Math.abs(failed.line! - before.line!), `failed save moved the target line from y=${before.line} to y=${failed.line}`).toBeLessThanOrEqual(1);
	await expect(page.getByPlaceholder('Comment…'), 'a failed save must keep the draft').toHaveValue('Preserve this viewport.');
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		const retry = page.getByRole('button', { name: 'Comment', exact: true }).last();
		await expect(retry).toBeVisible();
		expect(await retry.evaluate((button) => {
			const rect = button.getBoundingClientRect();
			return document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2) === button;
		}), 'the failure toast must not cover Comment').toBe(true);
	}
	await page.getByRole('button', { name: 'Comment', exact: true }).last().click();
	const submitted = await readerPosition();
	await expect(page.getByText('Preserve this viewport.')).toBeVisible();
	await expect(targetLine.locator('.cm-comment-anchor'), 'the saved comment must anchor on the selected line').toHaveCount(1);
	await expect(page.getByText("Couldn't post the comment — try again"), 'a successful retry must leave no stale failure toast').toHaveCount(0);
	const after = await readerPosition();
	await testInfo.attach(`comment-save-stages-${testInfo.project.name}`, { body: JSON.stringify({ before, composerOpen, failed, submitted, after }, null, 2), contentType: 'application/json' });
	await testInfo.attach(`comment-save-${testInfo.project.name}`, { body: await page.screenshot(), contentType: 'image/png' });
	expect(after.page, `page scroll moved from ${before.page} to ${after.page}`).toBe(before.page);
	// The first comment adds a status row above the editor, so the pane's
	// scrollTop may change; what the reader sees is where the line sits.
	expect(Math.abs(after.line! - before.line!), `target line moved from y=${before.line} to y=${after.line}`).toBeLessThanOrEqual(1);
});
