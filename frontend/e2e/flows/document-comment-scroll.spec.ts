import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN, watchSessionSubscriptions } from '../support/corsia';

test('@flow:document-comment-save preserves the visible editor viewport', async ({ page, request }, testInfo) => {
	test.setTimeout(60_000);
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
	const placeTarget = async () => {
		await expect.poll(async () => {
			if (await targetLine.count()) return true;
			await viewport.evaluate((element) => element.scrollBy(0, element.clientHeight / 2));
			return false;
		}, { message: 'target line never rendered while scrolling the document viewport', intervals: [50], timeout: 15_000 }).toBe(true);
		// Leave the line in the upper part of the viewport rather than where
		// Playwright's scrolling centers it, so a save-time recenter is visible.
		await targetLine.scrollIntoViewIfNeeded();
		await expect(targetLine).toBeVisible();
		const offset = (await targetLine.boundingBox())!.y - (await viewport.boundingBox())!.y - 120;
		await viewport.evaluate((element, offset) => element.scrollBy(0, offset), offset);
		await expect.poll(async () => Math.abs((await targetLine.boundingBox())!.y - (await viewport.boundingBox())!.y - 120) <= 2).toBe(true);
	};
	await placeTarget();
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
		// A jump away from the target leaves its line unrendered; report that rather than hang on it.
		lineBox: await targetLine.boundingBox({ timeout: 2_000 }).catch(() => null),
		line: (await targetLine.boundingBox({ timeout: 2_000 }).catch(() => null))?.y ?? null,
	});
	const codeMirrorHead = async () => page.locator('.cm-editor').evaluate((editor) => {
		const content = editor.querySelector('.cm-content') as HTMLElement & { cmTile?: { root?: { view?: { state?: { selection?: { main?: { head?: number } } } } } } } | null;
		const head = content?.cmTile?.root?.view?.state?.selection?.main?.head;
		// Fail loudly if CodeMirror reshapes these internals; a null head would make every caret comparison pass blind.
		if (typeof head !== 'number') throw new Error('CodeMirror selection head is unreadable through cmTile.root.view');
		return head;
	});
	// The reader has two carets: CodeMirror's own selection and the DOM selection. Opening a popover or drawer
	// moves focus out of the editor and the browser then clears the DOM selection (WebKit does), so the DOM
	// caret that must come back is the last one the editor itself held.
	await page.evaluate(() => {
		const win = window as unknown as { __editorCaret: unknown };
		document.addEventListener('selectionchange', () => {
			const selection = document.getSelection();
			const node = selection?.anchorNode;
			const element = node instanceof Element ? node : node?.parentElement;
			if (!element?.closest('.cm-content')) return;
			win.__editorCaret = { line: element.closest('.cm-line')?.textContent ?? null, offset: selection?.anchorOffset ?? null };
		});
	});
	const domCaret = async () => page.evaluate(() => {
		const selection = document.getSelection();
		const node = selection?.anchorNode;
		const element = node instanceof Element ? node : node?.parentElement;
		return { line: element?.closest('.cm-line')?.textContent ?? null, offset: selection?.anchorOffset ?? null };
	});
	const caret = async () => ({ head: await codeMirrorHead(), dom: await domCaret() });
	type Caret = Awaited<ReturnType<typeof caret>>;
	// The caret the reader had in the editor just before something took focus away from it.
	const caretBeforeFocusLeft = async (): Promise<Caret> => ({
		head: await codeMirrorHead(),
		dom: (await page.evaluate(() => (window as unknown as { __editorCaret: Caret['dom'] | null }).__editorCaret)) ?? await domCaret(),
	});
	const lifecyclePosition = async () => ({ viewport: await viewport.evaluate((element) => element.scrollTop), head: await codeMirrorHead() });
	const nextFrame = async () => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
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
	// Phone comments use the adaptive drawer, not the desktop popover focus path under regression here.
	if (testInfo.project.name === 'chromium-mobile') return;

	const anchor = targetLine.locator('.cm-comment-anchor');
	const popover = page.locator('[data-popover]');
	const clickDocumentText = async () => {
		const point = await targetLine.evaluate((line) => {
			const box = line.getBoundingClientRect();
			for (let x = box.right - 4; x > box.left; x -= 4) {
				const hit = document.elementFromPoint(x, box.top + box.height / 2);
				if (hit?.closest('.cm-line') === line && !hit.closest('.cm-comment-anchor')) return { x, y: box.top + box.height / 2 };
			}
			throw new Error('no unannotated text is available in the visible commented line');
		});
		await page.mouse.click(point.x, point.y);
	};
	const clickDifferentDocumentText = async () => {
		const point = await scroller.evaluate((scrollNode) => {
			let viewport: HTMLElement | null = scrollNode as HTMLElement;
			while (viewport) {
				const { overflowY } = getComputedStyle(viewport);
				if ((overflowY === 'auto' || overflowY === 'scroll') && viewport.scrollHeight > viewport.clientHeight) break;
				viewport = viewport.parentElement;
			}
			const visible = viewport?.getBoundingClientRect() ?? scrollNode.getBoundingClientRect();
			for (const line of scrollNode.querySelectorAll<HTMLElement>('.cm-line')) {
				const box = line.getBoundingClientRect();
				if (box.top < visible.top || box.bottom > visible.bottom || line.querySelector('.cm-comment-anchor')) continue;
				for (let x = box.left + 32; x < box.right - 4; x += 8) {
					const hit = document.elementFromPoint(x, box.top + box.height / 2);
					if (hit?.closest('.cm-line') === line) return { x, y: box.top + box.height / 2, text: line.textContent };
				}
			}
			throw new Error('no visible unannotated document line is available');
		});
		await page.mouse.click(point.x, point.y);
		return point.text;
	};
	const dismisses = [
		{
			name: 'unannotated text on the commented line',
			dismiss: clickDocumentText,
			assertCaret: async () => expect.poll(() => page.evaluate(() => {
				const node = document.getSelection()?.anchorNode;
				const element = node instanceof Element ? node : node?.parentElement;
				return element?.closest('.cm-line')?.textContent ?? null;
			})).toContain(target),
		},
		{
			name: 'Escape',
			dismiss: async () => page.keyboard.press('Escape'),
			// Escape only closes the popover: the reader's caret stays where it was when the popover opened.
			assertCaret: async (before: Caret) => expect.poll(caret, { message: 'Escape must keep the CodeMirror head and DOM caret' }).toEqual(before),
		},
		{
			name: 'empty canvas',
			dismiss: async () => scroller.click({ position: { x: 8, y: (await scroller.boundingBox())!.height - 8 } }),
			assertCaret: async () => {},
		},
	];
	const dismissResults: Array<{ trigger: string; before: Awaited<ReturnType<typeof readerPosition>>; after: Awaited<ReturnType<typeof readerPosition>> }> = [];
	for (const trigger of dismisses) {
		await placeTarget();
		await targetLine.click();
		await anchor.click();
		await expect(popover).toBeVisible();
		const beforeDismiss = await readerPosition();
		const caretBeforeDismiss = await caretBeforeFocusLeft();
		await trigger.dismiss();
		await expect(popover).toHaveCount(0);
		await nextFrame();
		await nextFrame();
		await trigger.assertCaret(caretBeforeDismiss);
		const afterDismiss = await readerPosition();
		dismissResults.push({ trigger: trigger.name, before: beforeDismiss, after: afterDismiss });
		expect(Math.abs(afterDismiss.viewport - beforeDismiss.viewport), `${trigger.name} changed reader scrollTop from ${beforeDismiss.viewport} to ${afterDismiss.viewport}`).toBeLessThanOrEqual(2);
	}
	await testInfo.attach(`comment-dismiss-scroll-${testInfo.project.name}`, { body: JSON.stringify(dismissResults, null, 2), contentType: 'application/json' });

	const resolveButton = () => popover.getByRole('button', { name: 'Resolve' });
	const resolveThread = async () => {
		await resolveButton().click();
		// Resolving removes the inline anchor, so the still-open popover is intentionally hidden.
		await expect(popover.locator('.ct.resolved')).toHaveCount(1);
	};
	const reopenThread = async (surface: import('@playwright/test').Locator) => {
		await surface.locator('.ct-res-head').click();
		await surface.getByRole('button', { name: 'Re-open' }).click();
	};
	let clickedDocumentLine = '';
	const lifecycleDismisses = [
		{ name: 'document line', dismiss: async () => { clickedDocumentLine = await clickDifferentDocumentText(); }, assertCaret: async () => expect.poll(() => page.evaluate(() => {
			const node = document.getSelection()?.anchorNode;
			const element = node instanceof Element ? node : node?.parentElement;
			return element?.closest('.cm-line')?.textContent ?? null;
		})).toContain(clickedDocumentLine) },
		{ name: 'empty canvas', dismiss: async () => scroller.click({ position: { x: 8, y: (await scroller.boundingBox())!.height - 8 } }), assertCaret: async () => {} },
		{
			name: 'Escape',
			dismiss: async () => page.keyboard.press('Escape'),
			assertCaret: async (opened: Caret) => expect.poll(caret, { message: 'Escape must keep the CodeMirror head and DOM caret' }).toEqual(opened),
		},
	];
	const lifecycleResults: Array<{ action: string; trigger: string; before: Awaited<ReturnType<typeof lifecyclePosition>>; afterAction: Awaited<ReturnType<typeof lifecyclePosition>>; frameOne: Awaited<ReturnType<typeof lifecyclePosition>>; frameTwo: Awaited<ReturnType<typeof lifecyclePosition>>; afterDismiss: Awaited<ReturnType<typeof lifecyclePosition>>; dismissFrameOne: Awaited<ReturnType<typeof lifecyclePosition>>; dismissFrameTwo: Awaited<ReturnType<typeof lifecyclePosition>> }> = [];
	for (const trigger of lifecycleDismisses) {
		// Reproduce the desktop sequence: leave the document, reopen its comment,
		// then remove the focused Resolve control before clicking the document.
		await placeTarget();
		await targetLine.click();
		await anchor.click();
		await expect(popover).toBeVisible();
		const beforeResolve = await lifecyclePosition();
		const caretBeforeResolve = await caretBeforeFocusLeft();
		await resolveThread();
		const resolveAfterAction = await lifecyclePosition();
		await nextFrame();
		const resolveFrameOne = await lifecyclePosition();
		await nextFrame();
		const resolveFrameTwo = await lifecyclePosition();
		await trigger.dismiss();
		await expect(popover).toHaveCount(0);
		await nextFrame();
		await nextFrame();
		await trigger.assertCaret(caretBeforeResolve);
		const resolveAfterDismiss = await lifecyclePosition();
		await nextFrame();
		const resolveDismissFrameOne = await lifecyclePosition();
		await nextFrame();
		const resolveDismissFrameTwo = await lifecyclePosition();
		lifecycleResults.push({ action: 'Resolve', trigger: trigger.name, before: beforeResolve, afterAction: resolveAfterAction, frameOne: resolveFrameOne, frameTwo: resolveFrameTwo, afterDismiss: resolveAfterDismiss, dismissFrameOne: resolveDismissFrameOne, dismissFrameTwo: resolveDismissFrameTwo });
		expect(Math.abs(resolveAfterDismiss.viewport - beforeResolve.viewport), `Resolve then ${trigger.name} changed reader scrollTop from ${beforeResolve.viewport} to ${resolveAfterDismiss.viewport}`).toBeLessThanOrEqual(2);
		expect(resolveDismissFrameOne.head, `Resolve then ${trigger.name} must keep the resulting CodeMirror selection head through one frame`).toBe(resolveAfterDismiss.head);
		expect(resolveDismissFrameTwo.head, `Resolve then ${trigger.name} must keep the resulting CodeMirror selection head through two frames`).toBe(resolveAfterDismiss.head);

		// Current code reopens a resolved thread from the persistent comment list,
		// not the now-removed inline anchor. That list never calls restoreEditor.
		// Measure the re-open from the reader's place, whatever the Resolve above did to it.
		await placeTarget();
		const caretBeforeDrawer = await caretBeforeFocusLeft();
		await page.getByRole('button', { name: /0 open threads/ }).click();
		const drawer = page.locator('[data-doc-comments-drawer]');
		await expect(drawer).toBeVisible();
		await drawer.getByRole('button', { name: 'Filter comments: Open' }).click();
		await drawer.getByRole('menuitemradio', { name: 'Resolved' }).click();
		await expect(drawer.locator('.ct-res-head')).toBeVisible();
		const beforeReopen = await lifecyclePosition();
		await reopenThread(drawer);
		const reopenAfterAction = await lifecyclePosition();
		await nextFrame();
		const reopenFrameOne = await lifecyclePosition();
		await nextFrame();
		const reopenFrameTwo = await lifecyclePosition();
		await drawer.getByRole('button', { name: 'Close comments' }).click();
		await expect(drawer).toHaveCount(0);
		await trigger.dismiss();
		await nextFrame();
		await nextFrame();
		// Escape leaves the drawer rather than returning to the editor. When the reader returns by keyboard focus, CodeMirror
		// must restore its full saved selection, including the DOM caret rebuilt with the resolved highlight.
		if (trigger.name === 'Escape') {
			await page.locator('.cm-content').focus();
			await expect.poll(caret, { message: 'Returning to the editor after Escape must restore the full caret' }).toEqual(caretBeforeDrawer);
		} else await trigger.assertCaret(caretBeforeDrawer);
		const reopenAfterDismiss = await lifecyclePosition();
		await nextFrame();
		const reopenDismissFrameOne = await lifecyclePosition();
		await nextFrame();
		const reopenDismissFrameTwo = await lifecyclePosition();
		lifecycleResults.push({ action: 'Re-open', trigger: trigger.name, before: beforeReopen, afterAction: reopenAfterAction, frameOne: reopenFrameOne, frameTwo: reopenFrameTwo, afterDismiss: reopenAfterDismiss, dismissFrameOne: reopenDismissFrameOne, dismissFrameTwo: reopenDismissFrameTwo });
		expect(Math.abs(reopenAfterDismiss.viewport - beforeReopen.viewport), `Re-open then ${trigger.name} changed reader scrollTop from ${beforeReopen.viewport} to ${reopenAfterDismiss.viewport}`).toBeLessThanOrEqual(2);
		expect(reopenDismissFrameOne.head, `Re-open then ${trigger.name} must keep the resulting CodeMirror selection head through one frame`).toBe(reopenAfterDismiss.head);
		expect(reopenDismissFrameTwo.head, `Re-open then ${trigger.name} must keep the resulting CodeMirror selection head through two frames`).toBe(reopenAfterDismiss.head);
	}
	await testInfo.attach(`comment-resolve-reopen-scroll-${testInfo.project.name}`, { body: JSON.stringify(lifecycleResults, null, 2), contentType: 'application/json' });
});
