import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, loadPastFirstRunLanding } from '../support/corsia';
import {
	chatSend, chatSendGroup, deliveredBatches, headerSend, heldIn, imageBarSend, installWorkspace,
	openThread, seedHeldComments, seedTextDocument, expectSenders, senderLines, sentToast, watchSendRequests, watchSenders, holdPersonBatches, turnOffDocumentComments, readDeliveryPreference, setDebugVersionOverlay, setMockCorsiaAgentModel, makeHeldBatchDue,
} from '../support/heldComments';
import {
	bar, commentOnRegion, expectBarFits, openDocument, openImage, overlay, putSheetAway, seedImageDocument,
} from '../support/docImageComments';

// Held-comment delivery as one composed surface (DCOL-FR13.12, R2.9): a held comment has exactly one sender per place.
// The document header sends the document-held lane, the image viewer bar sends the image-held lane, and the chat sends
// the thread's document-held comments only where their own header is not on screen.

const mockOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};

const framesOut = process.env.HELD_COMMENT_DELIVERY_FRAMES_OUT;
async function captureFrame(page: Page, state: string, viewport: '1440' | '393') {
	if (state !== 'toggle-on-sent') {
		expect((await readDeliveryPreference(page.request)).immediate, `${state}: held journeys have immediate delivery OFF`).toBe(false);
		const toggle = page.getByRole('button', { name: 'Deliver comments immediately', exact: true });
		if (await toggle.isVisible()) await expect(toggle).toHaveAttribute('aria-pressed', 'false');
	}
	if (!framesOut) return;
	mkdirSync(framesOut, { recursive: true });
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		await expect(page.locator('[data-wake-retry]'), `${state}: no unrelated undelivered wake`).toHaveCount(0);
		await expect(page.getByText('Stalled: its last Turn failed', { exact: true }), `${state}: no unrelated stalled plan`).toHaveCount(0);
		await expect(page.locator('.version-overlay'), `${state}: capture excludes developer provenance`).toHaveCount(0);
		await page.screenshot({ path: join(framesOut, `${state}-${viewport}-${theme}.png`), animations: 'disabled', scale: 'css' });
		const preference = (await readDeliveryPreference(page.request)).immediate;
		const evidence = await page.evaluate((preference) => {
			const marker = document.querySelector('.cm-comment-marker');
			const box = marker?.getBoundingClientRect();
			const ancestors = []; let parent = marker?.parentElement;
			while (parent) {
				const style = getComputedStyle(parent);
				if (/(hidden|clip|auto|scroll)/.test(style.overflowX)) {
					const p = parent.getBoundingClientRect();
					ancestors.push({ class: parent.className, left: p.left, right: p.right, overflowX: style.overflowX });
				}
				parent = parent.parentElement;
			}
			const tools = document.querySelector<HTMLElement>('.document-tools');
			const header = tools?.closest<HTMLElement>('.document-header');
			const headerBox = header?.getBoundingClientRect();
			const headerStyle = header ? getComputedStyle(header) : null;
			const headerTools = tools && headerBox && headerStyle ? {
				contentLeft: headerBox.left + parseFloat(headerStyle.paddingLeft),
				contentRight: headerBox.right - parseFloat(headerStyle.paddingRight),
				controls: [...tools.querySelectorAll<HTMLElement>('.sending-now .btn, [aria-label="Deliver comments immediately"], .comment-status, .review-request')]
					.map((control) => {
						const controlBox = control.getBoundingClientRect();
						return { label: control.getAttribute('aria-label') ?? control.textContent?.trim(), left: controlBox.left, right: controlBox.right };
					}),
			} : null;
			return {
				preference,
				toggle: document.querySelector('[aria-label="Deliver comments immediately"]')?.getAttribute('aria-pressed'),
				toast: document.querySelector('.toast')?.textContent,
				headerTools,
				marker: box ? { text: marker?.textContent, left: box.left, right: box.right, ancestors } : null,
			};
		}, preference);
		writeFileSync(join(framesOut, `${state}-${viewport}-${theme}.json`), JSON.stringify(evidence, null, 2));
	}
}

/** The chat's Send answers the first reading; an absence is only true once that reading has landed. */
async function settledSenders(page: Page): Promise<string[]> {
	await expect(page.locator('.document-header, .chat-attention-stack').first()).toBeAttached();
	await page.waitForTimeout(400);
	return senderLines(page);
}

test.describe('@flow:held-comment-delivery @surface:documents held comments have one sender per place', () => {
	test.beforeEach(async ({ page, request }) => {
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		await setMockCorsiaAgentModel(request);
		await setDebugVersionOverlay(request, false);
	});
	for (const width of [1440, 393] as const) test(`document journey: toggle off, post, Send and turn immediate delivery back on (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
		const title = `held-journey-${Date.now()}`;
		const threadId = await createSession(request, title);
		const longDocument = ['A commentable document.', ...Array.from({ length: 28 }, (_, index) => `Paragraph ${index + 1} with enough text to fill the canvas behind the transient delivery notice.`)].join('\n\n');
		const doc = await seedTextDocument(request, threadId, title, 'journey', longDocument);
		await installWorkspace(page, threadId, [doc]);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);
		await captureFrame(page, 'toggle-off-header', String(width) as '1440' | '393');
		const sends = watchSendRequests(page);
		const post = async (body: string) => {
			const line = page.locator('.cm-line').first();
			await line.click();
			const ends = await line.evaluate((element) => {
				const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
				let node: Node | null;
				while ((node = walker.nextNode())) {
					const start = (node.textContent ?? '').indexOf('commentable');
					if (start < 0) continue;
					const range = document.createRange();
					range.setStart(node, start); range.setEnd(node, start + 'commentable'.length);
					const rect = range.getBoundingClientRect();
					return { x: rect.left + 1, end: rect.right - 1, y: rect.top + rect.height / 2 };
				}
				throw new Error('Expected commentable text');
			});
			await page.mouse.move(ends.x, ends.y);
			await page.mouse.down();
			await page.mouse.move(ends.end, ends.y, { steps: 12 });
			await page.mouse.up();
			await page.locator('.sel-comment').click();
			await page.getByPlaceholder('Comment…').fill(body);
			await page.getByRole('button', { name: 'Comment', exact: true }).last().click();
			await expect(headerSend(page)).toHaveText('Send 1 comment');
			await putSheetAway(page);
		};
		await post('Please clarify this sentence.');
		expect(heldIn(doc.artifactId, 'document-held')).toBe(1);
		expect(sends.document).toHaveLength(0);
		await captureFrame(page, 'document-comment-held', String(width) as '1440' | '393');
		await headerSend(page).click();
		await expect(sentToast(page, '1 comment sent')).toBeVisible();
		await captureFrame(page, 'document-send-toast', String(width) as '1440' | '393');
		await sentToast(page, '1 comment sent').getByRole('button', { name: 'Dismiss', exact: true }).click();
		if (width === 1440) await page.keyboard.press('Escape');
		await post('Please check this revision too.');
		await page.getByRole('button', { name: 'Deliver comments immediately', exact: true }).click();
		await expect(sentToast(page, '1 comment sent')).toBeVisible();
		await expect(headerSend(page)).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Deliver comments immediately', exact: true })).toHaveAttribute('aria-pressed', 'true');
		expect(sends.document.map((body) => JSON.parse(body).delivery_policy)).toEqual(['document-held', 'document-held']);
		expect(heldIn(doc.artifactId, 'document-held')).toBe(0);
		expect(deliveredBatches(doc.artifactId, 'document-held')).toBe(2);
		await captureFrame(page, 'toggle-on-sent', String(width) as '1440' | '393');
	});

	test('a sole held document that is open has its header as the only sender (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `held-sole-${Date.now()}`;
		const threadId = await createSession(request, title);
		const doc = await seedTextDocument(request, threadId, title, 'a');
		await seedHeldComments(request, doc.artifactId, 2);
		await installWorkspace(page, threadId, [doc]);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);
		await expect(headerSend(page)).toBeVisible();
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		expect(await settledSenders(page), 'the header is the one sender; the chat offers none').toEqual(['header: Send 2 comments']);
		await expect(chatSendGroup(page)).toHaveCount(0);
		await captureFrame(page, 'header-sole', '1440');
	});

	test('one held document that is not on screen is sent from the chat: another tab, a collapsed canvas (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `held-hidden-${Date.now()}`;
		const threadId = await createSession(request, title);
		const held = await seedTextDocument(request, threadId, title, 'held');
		const other = await seedTextDocument(request, threadId, title, 'other');
		await seedHeldComments(request, held.artifactId, 3);
		await installWorkspace(page, threadId, [held, other], 1);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);

		// Another tab is active: the held document's header is not on screen, so the chat is its sender.
		await expect(chatSend(page)).toHaveText('Send 3 comments');
		await expect(headerSend(page)).toHaveCount(0);
		await expectSenders(page, ['chat: Send 3 comments']);
		await expect(page.locator('.cm-content')).toContainText('A commentable document.');
		await captureFrame(page, 'chat-other-active-tab', '1440');

		// Its own tab: the header is the sender and the chat's Send leaves.
		await page.getByRole('tab', { name: held.fileName }).click();
		await expect(headerSend(page)).toHaveText('Send 3 comments');
		await expect(chatSendGroup(page)).toHaveCount(0);
		await expectSenders(page, ['header: Send 3 comments']);

		// A collapsed canvas shows no header: the chat is the sender again.
		await page.getByRole('button', { name: 'Collapse Files' }).click();
		await expect(chatSend(page)).toHaveText('Send 3 comments');
		await expectSenders(page, ['chat: Send 3 comments']);
		await captureFrame(page, 'chat-collapsed-canvas', '1440');
		await page.getByRole('button', { name: /^Expand Files/ }).click();
		await expect(headerSend(page)).toHaveText('Send 3 comments');
		await expect(chatSendGroup(page)).toHaveCount(0);
	});

	test('several held documents, equal counts included, read Send all in the chat beside each document\'s own Send (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `held-several-${Date.now()}`;
		const threadId = await createSession(request, title);
		const first = await seedTextDocument(request, threadId, title, 'first');
		const second = await seedTextDocument(request, threadId, title, 'second');
		await seedHeldComments(request, first.artifactId, 2, 'First');
		await seedHeldComments(request, second.artifactId, 2, 'Second');
		await installWorkspace(page, threadId, [first, second], 0);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);
		// Two documents each hold 2: the same count is two batches, so the chat is not suppressed by the equal number.
		await expect(chatSend(page)).toHaveText('Send all 4 comments');
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		await expectSenders(page, ['chat: Send all 4 comments', 'header: Send 2 comments']);
		await captureFrame(page, 'chat-several-artifacts', '1440');

		// The header sends its own document; the chat then reads the one that is left, which is not on screen.
		await headerSend(page).click();
		await expect(sentToast(page, '2 comments sent')).toBeVisible();
		await sentToast(page, '2 comments sent').getByRole('button', { name: 'Dismiss', exact: true }).click();
		await expect(headerSend(page)).toHaveCount(0);
		await expect(chatSend(page)).toHaveText('Send 2 comments');
		expect(heldIn(first.artifactId, 'document-held'), 'the first document sent').toBe(0);
		expect(heldIn(second.artifactId, 'document-held'), 'the second document still holds its two').toBe(2);
		await chatSend(page).click();
		// The canvas that could host a local confirmation is off screen; chat Send clears itself without a global success toast.
		await expect(sentToast(page, '2 comments sent')).toHaveCount(0);
		await expect(chatSendGroup(page)).toHaveCount(0);
		await expectSenders(page, []);
		expect([deliveredBatches(first.artifactId, 'document-held'), deliveredBatches(second.artifactId, 'document-held')]).toEqual([1, 1]);
	});

	test('an image over a held document keeps the chat as its document sender, and the bar sends only the image (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(120_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImageDocument(page, request, 'held-overlay', null);
		await seedHeldComments(request, seeded.artifactId, 2, 'Overlay', { text: 'Design', from: 2, to: 8 });
		await openDocument(page, seeded, 1440);
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		expect(await settledSenders(page), 'the open document\'s header is its only sender').toEqual(['header: Send 2 comments']);

		// The viewer covers the header, so the chat is the sender for the document's comments.
		await openImage(page);
		await expect(chatSend(page)).toHaveText('Send 2 comments');
		await expectSenders(page, ['chat: Send 2 comments'], 'a covered header is no sender; no image comment yet');

		// Three pins: one image-bar Send for exactly the image's three.
		for (const [index, at] of [{ x: 0.05, y: 0.05 }, { x: 0.6, y: 0.1 }, { x: 0.1, y: 0.55 }].entries()) {
			await commentOnRegion(page, `Pin ${index + 1}`, at);
		}
		await expect(imageBarSend(page)).toHaveText('Send 3 comments');
		await expectSenders(page, ['chat: Send 2 comments', 'image-bar: Send 3 comments']);
		await captureFrame(page, 'image-bar-send-3', '1440');
		await expectBarFits(page);

		// The chat sends the document lane only; its off-screen canvas has no local host, so it stays toast-free.
		await chatSend(page).click();
		await expect(sentToast(page, '2 comments sent')).toHaveCount(0);
		await expect(chatSendGroup(page)).toHaveCount(0);
		await expect(imageBarSend(page)).toHaveText('Send 3 comments');
		expect([heldIn(seeded.artifactId, 'document-held'), heldIn(seeded.artifactId, 'image-held')]).toEqual([0, 3]);

		// The bar sends the image lane only.
		await imageBarSend(page).click();
		await expect(sentToast(page, '3 comments sent')).toBeVisible();
		await expect(imageBarSend(page)).toHaveCount(0);
		expect(heldIn(seeded.artifactId, 'image-held')).toBe(0);
		expect(deliveredBatches(seeded.artifactId, 'image-held')).toBe(1);
	});

	test('leaving an image sends its comments once and the document\'s count never flashes the image\'s (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(120_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImageDocument(page, request, 'held-leave', null);
		await seedHeldComments(request, seeded.artifactId, 2, 'Leave', { text: 'Design', from: 2, to: 8 });
		const sends = watchSendRequests(page);
		await openDocument(page, seeded, 1440);
		await openImage(page);
		for (const [index, at] of [{ x: 0.05, y: 0.05 }, { x: 0.6, y: 0.1 }, { x: 0.1, y: 0.55 }].entries()) {
			await commentOnRegion(page, `Leave pin ${index + 1}`, at);
		}
		await expect(imageBarSend(page)).toHaveText('Send 3 comments');

		const watch = await watchSenders(page);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(sentToast(page, '3 comments sent')).toBeVisible();
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		await expect(chatSendGroup(page)).toHaveCount(0);
		await captureFrame(page, 'post-image-close-confirmation', '1440');
		await sentToast(page, '3 comments sent').getByRole('button', { name: 'Dismiss', exact: true }).click();
		await expect(sentToast(page, '3 comments sent')).toHaveCount(0);
		await captureFrame(page, 'post-image-close', '1440');
		const seen = await watch.stop();
		// Every Send a person could have seen from the click to the end named its own lane's count, never 3 or 5 in the header.
		expect(seen.filter((line) => line.startsWith('header:')), JSON.stringify(seen)).toEqual(['header: Send 2 comments']);
		expect(seen, JSON.stringify(seen)).not.toContain('chat: Send 5 comments');
		expect(sends.document.map((body) => JSON.parse(body).delivery_policy), 'leaving sent the image lane, once').toEqual(['image-held']);
		expect(sends.thread).toEqual([]);
		expect([heldIn(seeded.artifactId, 'image-held'), heldIn(seeded.artifactId, 'document-held')]).toEqual([0, 2]);
	});

	test('the header and the chat Send at once deliver each document once (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width: 1440, height: 900 });
		const title = `held-nested-${Date.now()}`;
		const threadId = await createSession(request, title);
		const first = await seedTextDocument(request, threadId, title, 'first');
		const second = await seedTextDocument(request, threadId, title, 'second');
		await seedHeldComments(request, first.artifactId, 2, 'First');
		await seedHeldComments(request, second.artifactId, 2, 'Second');
		await installWorkspace(page, threadId, [first, second], 0);
		const claimed: number[] = [];
		page.on('response', async (response) => {
			const pathname = new URL(response.url()).pathname;
			if (response.request().method() !== 'POST' || !['/api/doc-feedback/send', '/api/doc-feedback/thread/send'].includes(pathname)) return;
			claimed.push(((await response.json()) as { sent_count?: number }).sent_count ?? 0);
		});
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);
		await expect(chatSend(page)).toHaveText('Send all 4 comments');
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		const [header, chat] = await Promise.all([headerSend(page).elementHandle(), chatSend(page).elementHandle()]);
		// Both senders in one task: whichever the server takes first delivers, and the other finds nothing left.
		await page.evaluate(([a, b]) => { (a as HTMLElement).click(); (b as HTMLElement).click(); }, [header, chat]);
		await expect(chatSendGroup(page)).toHaveCount(0);
		await expect(headerSend(page)).toHaveCount(0);
		await expect.poll(() => claimed.length).toBe(2);
		expect(claimed.reduce((sum, count) => sum + count, 0), `each comment is claimed by exactly one send: ${JSON.stringify(claimed)}`).toBe(4);
		await expect(page.locator('.toast').filter({ hasText: "Couldn't send" })).toHaveCount(0);
		for (const document of [first, second]) {
			expect(deliveredBatches(document.artifactId, 'document-held'), `${document.fileName} delivered once`).toBe(1);
			expect(heldIn(document.artifactId, 'document-held')).toBe(0);
		}
	});

	for (const width of [1440, 393] as const) test(`a reload that interrupts leaving an image is recovered when its document mounts (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(120_000);
		await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
		const seeded = await seedImageDocument(page, request, 'held-reload', null, undefined, true);
		const sends = watchSendRequests(page);
		await openDocument(page, seeded, width);
		await openImage(page);
		await commentOnRegion(page, 'Reload pin one', { x: 0.05, y: 0.05 });
		await putSheetAway(page);
		await commentOnRegion(page, 'Reload pin two', { x: 0.6, y: 0.1 });
		await putSheetAway(page);
		await expect(imageBarSend(page)).toHaveText('Send 2 comments');
		expect(heldIn(seeded.artifactId, 'image-held')).toBe(2);

		// The page goes before leaving the image can finish: the teardown send, if it fires, never reaches the server.
		let aborted = 0;
		await page.route('**/api/doc-feedback/send', async (route) => {
			if (aborted === 0) { aborted += 1; await route.abort(); } else await route.continue();
		});
		await loadPastFirstRunLanding(page, () => page.reload(), { lands: true });
		if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();

		// A reload lands on the workspace home, not on the conversation; opening the conversation restores the
		// document's canvas tab (the image viewer itself is not restored) and the remainder is sent as it mounts.
		await page.getByText(seeded.sessionTitle).first().click();
		await expect(page.locator('.document-header')).toBeVisible();
		await expect(overlay(page)).toHaveCount(0);
		await expect(sentToast(page, '2 comments sent')).toBeVisible();
		await expect.poll(() => heldIn(seeded.artifactId, 'image-held')).toBe(0);
		await captureFrame(page, 'reload-containing-document-remount', String(width) as '1440' | '393');
		expect(deliveredBatches(seeded.artifactId, 'image-held'), 'the remainder was delivered once').toBe(1);
		expect(sends.document.map((body) => JSON.parse(body).delivery_policy).every((policy) => policy === 'image-held'), JSON.stringify(sends)).toBe(true);
		await expect(imageBarSend(page)).toHaveCount(0);
	});

	test('image-bar Send 3 and its post-close truth fit at 393', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(120_000);
		await page.setViewportSize({ width: 393, height: 852 });
		const seeded = await seedImageDocument(page, request, 'held-phone-image', null);
		await seedHeldComments(request, seeded.artifactId, 2, 'Document', { text: 'Design', from: 2, to: 8 });
		await openDocument(page, seeded, 393);
		await openImage(page);
		for (const [index, at] of [{ x: 0.05, y: 0.05 }, { x: 0.6, y: 0.1 }, { x: 0.1, y: 0.55 }].entries()) {
			await commentOnRegion(page, `Phone pin ${index + 1}`, at);
			await putSheetAway(page);
		}
		await expect(imageBarSend(page)).toHaveText('Send 3 comments');
		await expectOneLine(imageBarSend(page), 393);
		await expectBarFits(page);
		await captureFrame(page, 'image-bar-send-3', '393');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(headerSend(page)).toHaveText('Send 2 comments');
		await expect.poll(() => heldIn(seeded.artifactId, 'image-held')).toBe(0);
		expect(deliveredBatches(seeded.artifactId, 'image-held')).toBe(1);
		await expect(sentToast(page, '3 comments sent')).toBeVisible();
		await captureFrame(page, 'post-image-close-confirmation', '393');
		await sentToast(page, '3 comments sent').getByRole('button', { name: 'Dismiss', exact: true }).click();
		await expect(sentToast(page, '3 comments sent')).toHaveCount(0);
		await captureFrame(page, 'post-image-close', '393');
	});

	test('image-bar Send 101 fits at 393 and sends only its image lane', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 393, height: 852 });
		const seeded = await seedImageDocument(page, request, 'held-phone-image-many', null);
		await seedHeldComments(request, seeded.artifactId, 2, 'Document', { text: 'Design', from: 2, to: 8 });
		await openDocument(page, seeded, 393);
		await openImage(page);
		holdPersonBatches(true);
		try {
			// Seed the first 100 through live capture/POST; the last UI comment also refreshes the mounted viewer.
			await page.evaluate(async (artifactId) => {
				const { liveDocumentBindings } = await import('/src/lib/stores/liveDocuments.ts');
				const { addDocImageComment } = await import('/src/lib/stores/imageRegionComments.ts');
				let bindings: Record<string, import('../../src/lib/stores/liveDocumentSession').LiveDocumentSession> = {};
				const stop = liveDocumentBindings.subscribe((value) => { bindings = value; });
				stop();
				const session = Object.values(bindings).find((bound) => bound.artifactId === artifactId);
				if (!session) throw new Error('The open image must have its containing live document');
				const text = session.text.toString(), quote = '![Mock screen](shots/screen.png)', from = text.indexOf(quote);
				if (from < 0) throw new Error('The live document must contain the seeded image');
				for (let index = 1; index <= 100; index++) {
					await addDocImageComment({ artifactId, from, to: from + quote.length, text, session },
						{ x: 0.2, y: 0.25, w: 0.1, h: 0.1 }, `Many image pin ${index}`);
				}
			}, seeded.artifactId);
			await commentOnRegion(page, 'Many image pin 101', { x: 0.6, y: 0.1 });
			await putSheetAway(page);
		} finally { holdPersonBatches(false); }
		await expect(imageBarSend(page)).toHaveText('Send 101 comments');
		await expectOneLine(imageBarSend(page), 393);
		await expectBarFits(page);
		await captureFrame(page, 'image-bar-send-101', '393');
		await imageBarSend(page).click();
		await expect.poll(() => heldIn(seeded.artifactId, 'image-held')).toBe(0);
		expect(heldIn(seeded.artifactId, 'document-held')).toBe(2);
		expect(deliveredBatches(seeded.artifactId, 'image-held')).toBe(1);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(headerSend(page)).toHaveText('Send 2 comments');
	});

	for (const width of [1440, 393] as const) test(`held Send stays below an occupied decision stack (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
		const title = `held-stack-${Date.now()}`;
		const threadId = await createSession(request, title);
		const doc = await seedTextDocument(request, threadId, title, 'stack');
		await seedHeldComments(request, doc.artifactId, 3);
		const detail = {
			id: 'held-stack-decision', plan_id: 'plan-1', node_id: 'node-1', source_node_id: 'node-1',
			thread_id: threadId, title: 'Choose the review scope', context: 'Choose what to review next.',
			tradeoff: 'Breadth or depth.', recommendation: 'A', rationale: 'Finish the current review.',
			options: [{ id: 'A', label: 'Current document', detail: 'Finish this review.' }, { id: 'B', label: 'All documents', detail: 'Review everything.' }],
			other_enabled: true, order_key: 0, revision: 1, raised_at: new Date().toISOString(),
			source: { thread_id: threadId, thread_label: 'Reviewer', project_id: null, project_label: null, archived: false, parent_reviewed: false },
		};
		const queued = { external_id: detail.id, renderer: 'decision', detail };
		await page.route('**/api/attention/queue', (route) => route.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify({ current: queued, count: 1, items: [queued] }),
		}));
		await installWorkspace(page, threadId, [doc], 0);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);
		if (width === 393) await page.getByRole('button', { name: 'Chat', exact: true }).click();
		else await page.getByRole('button', { name: 'Collapse Files' }).click();
		const dock = page.locator('.chat-attention-stack .attention-dock');
		await expect(dock).toBeVisible();
		await expect(chatSend(page)).toHaveText('Send 3 comments');
		await expect(chatSend(page)).toBeInViewport({ ratio: 1 });
		// The dock wrapper includes negative-margin shadow gutters; measure the actual decision action.
		const dockBox = (await dock.locator('.dock-bar').boundingBox())!;
		const sendBox = (await chatSend(page).boundingBox())!;
		expect(sendBox.y, 'decision remains above held comments').toBeGreaterThanOrEqual(dockBox.y + dockBox.height);
		await captureFrame(page, 'stack-priority-occupied', String(width) as '1440' | '393');
		await chatSend(page).click();
		await expect(chatSendGroup(page)).toHaveCount(0);
		await expect(dock).toBeVisible();
		expect(deliveredBatches(doc.artifactId, 'document-held')).toBe(1);
	});

	test('1, 3, and 99 held comments keep Send beside the header controls at chat-split and phone widths', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		for (const count of [1, 3, 99]) {
			await page.setViewportSize({ width: 1440, height: 900 });
			const title = `held-${count}-${Date.now()}-a-document-title-long-enough-to-prove-the-header-keeps-its-tools-together`;
			const threadId = await createSession(request, title);
			const doc = await seedTextDocument(request, threadId, title, `count-${count}`);
			await seedHeldComments(request, doc.artifactId, count);
			await installWorkspace(page, threadId, [doc], 0);
			await openThread(page, threadId, title);
			await turnOffDocumentComments(page);
			await expect(headerSend(page)).toHaveText(`Send ${count} comment${count === 1 ? '' : 's'}`);
			await expectHeaderToolsShareRow(page, 1440);
			await page.setViewportSize({ width: 393, height: 852 });
			await expectHeaderToolsShareRow(page, 393);
		}
	});

	test.describe('idle delivery', () => {
		test.use({ seedPerTest: true });

		test('document activity extends a held lane through the touch route', async ({ page, request }, testInfo) => {
			mockOnly(testInfo);
			const title = `held-touch-${Date.now()}`;
			const threadId = await createSession(request, title);
			const doc = await seedTextDocument(request, threadId, title, 'activity');
			await seedHeldComments(request, doc.artifactId, 1);
			await installWorkspace(page, threadId, [doc], 0);
			// Any pointer activity in the document touches it, and touches are throttled for 30 s, so the toggle click can
			// take the touch before the click below does. Record every touch response from the start and read the first.
			const touched: Array<Promise<unknown>> = [];
			page.on('response', (response) => {
				if (response.url().endsWith('/api/doc-feedback/touch') && response.request().method() === 'POST') touched.push(response.json());
			});
			await openThread(page, threadId, title);
			await turnOffDocumentComments(page);
			await expect(headerSend(page)).toHaveText('Send 1 comment');

			await page.locator('.document-canvas').click({ position: { x: 8, y: 8 } });
			await expect.poll(() => touched.length, 'the document activity touch').toBeGreaterThan(0);
			expect(await touched[0]).toEqual({ touched: 1 });
		});

		test('in an open document clears Send without a confirmation', async ({ page, request }, testInfo) => {
			mockOnly(testInfo);
			test.setTimeout(90_000);
			await page.setViewportSize({ width: 393, height: 852 });
			const title = `held-idle-${Date.now()}`;
			const threadId = await createSession(request, title);
			const doc = await seedTextDocument(request, threadId, title, 'idle');
			await seedHeldComments(request, doc.artifactId, 1);
			await installWorkspace(page, threadId, [doc], 0);
			const touches: string[] = [];
			page.on('response', (response) => { if (response.url().endsWith('/api/doc-feedback/touch') && response.request().method() === 'POST') touches.push(response.url()); });
			await openThread(page, threadId, title);
			await turnOffDocumentComments(page);
			await expect(headerSend(page)).toHaveText('Send 1 comment');
			// The document's first activity extends the batch's deadline, and activity is then throttled for 30 s. Take that
			// touch (this click, or one the page already made) before the batch is made due, so none lands after it.
			await page.locator('.document-canvas').click({ position: { x: 8, y: 8 } });
			await expect.poll(() => touches.length, 'the document activity touch').toBeGreaterThan(0);

			// Shorten the two-minute server deadline only in the disposable mock DB, then make a separate real feedback write
			// wake the continuation loop. The visible document receives the real doc_comment_changed socket event.
			makeHeldBatchDue(doc.artifactId, 'document-held');
			const witness = await seedTextDocument(request, threadId, `${title}-wake`, 'wake');
			await seedHeldComments(request, witness.artifactId, 1);
			await expect(headerSend(page)).toHaveCount(0);
			await expect(page.locator('.document-header .sent')).toHaveCount(0);
			await expect(page.locator('.document-canvas .delivery-confirmation')).toHaveCount(0);
			expect(deliveredBatches(doc.artifactId, 'document-held')).toBe(1);
		});
	});

	test('101 held comments keep one line: the header at 393, then the chat at 393 and 1440', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 393, height: 852 });
		const title = `held-many-${Date.now()}-a-document-title-long-enough-to-prove-the-header-keeps-its-tools-together`;
		const threadId = await createSession(request, title);
		const doc = await seedTextDocument(request, threadId, title, 'many');
		await seedHeldComments(request, doc.artifactId, 101);
		await installWorkspace(page, threadId, [doc], 0);
		await openThread(page, threadId, title);
		await turnOffDocumentComments(page);

		// On a phone the canvas is the whole screen: the header's Send, on one line, inside it.
		await expect(headerSend(page)).toHaveText('Send 101 comments');
		await expectOneLine(headerSend(page), 393);
		await expectHeaderToolsShareRow(page, 393);
		await captureFrame(page, 'header-101', '393');
		await expectSenders(page, ['header: Send 101 comments']);

		// Back to chat: the document is no longer on screen, so the chat is the sender, on one line.
		await page.getByRole('button', { name: 'Chat', exact: true }).click();
		await expect(chatSend(page)).toHaveText('Send 101 comments');
		await expectOneLine(chatSend(page), 393);
		await captureFrame(page, 'chat-101', '393');
		await expectSenders(page, ['chat: Send 101 comments']);
		await page.setViewportSize({ width: 1440, height: 900 });
		await expectOneLine(headerSend(page), 1440);
		await expectHeaderToolsShareRow(page, 1440);
		await captureFrame(page, 'header-101', '1440');
		await page.getByRole('button', { name: 'Collapse Files' }).click();
		await expectOneLine(chatSend(page), 1440);
		await captureFrame(page, 'chat-101', '1440');
	});
});

/** Send and the document controls form one compact row at every supported canvas width. */
async function expectHeaderToolsShareRow(page: Page, width: number) {
	const send = headerSend(page);
	const toggle = page.getByRole('button', { name: 'Deliver comments immediately', exact: true });
	const comments = page.locator('.document-tools .comment-status');
	const review = page.locator('.document-tools .review-request');
	const toggleLabel = page.locator('.document-tools .delivery-toggle > span');
	await expect(send).toBeVisible();
	await expect(toggle).toBeVisible();
	await expect(toggleLabel).toHaveText('Deliver immediately');
	await expect(comments).toBeVisible();
	await page.waitForTimeout(400);
	const result = await page.locator('.document-tools').evaluate((tools, selectors) => {
		const send = tools.querySelector<HTMLElement>(selectors.send);
		const toggle = tools.querySelector<HTMLElement>(selectors.toggle);
		const comments = tools.querySelector<HTMLElement>(selectors.comments);
		const review = tools.querySelector<HTMLElement>(selectors.review);
		const row = [send, toggle, comments, review].filter((item): item is HTMLElement => !!item).map((item) => item.getBoundingClientRect());
		const header = tools.closest<HTMLElement>('.document-header');
		if (!header) throw new Error('Expected document header');
		const headerBox = header.getBoundingClientRect();
		const headerStyle = getComputedStyle(header);
		return {
			containsSend: !!send,
			wrap: getComputedStyle(tools).flexWrap,
			centers: row.map((box) => box.top + box.height / 2),
			controls: row.map((box) => ({ left: box.left, right: box.right })),
			contentLeft: headerBox.left + parseFloat(headerStyle.paddingLeft),
			contentRight: headerBox.right - parseFloat(headerStyle.paddingRight),
		};
	}, { send: '.sending-now .btn', toggle: '[aria-label="Deliver comments immediately"]', comments: '.comment-status', review: '.review-request' });
	expect(result.containsSend, 'Send is inside the document tools row').toBe(true);
	expect(result.wrap, 'the compact tools row does not wrap').toBe('nowrap');
	expect(Math.max(...result.centers) - Math.min(...result.centers), 'Send, count, toggle, and review are vertically centered as one row').toBeLessThanOrEqual(2);
	expect(result.controls.every((box) => box.left >= result.contentLeft - 0.5), 'each control starts inside the header content edge').toBe(true);
	expect(result.controls.every((box) => box.right <= result.contentRight + 0.5), 'each control ends inside the header content edge').toBe(true);
}

/** A Send reads on one line, whole inside the screen: its label has one rendered line and nothing scrolls sideways. */
async function expectOneLine(send: Locator, width: number) {
	await expect(send).toBeInViewport({ ratio: 0.9 });
	const fit = await send.evaluate((button) => {
		const range = document.createRange();
		range.selectNodeContents(button);
		const box = button.getBoundingClientRect();
		const text = range.getBoundingClientRect();
		const shortLabel = getComputedStyle(button, '::before').content;
		return {
			// Narrow canvas buttons intentionally render data-short in ::before while retaining the full DOM text for accessibility.
			visibleLabel: shortLabel === 'none' ? button.textContent?.trim() : shortLabel.replaceAll('"', ''),
			lines: new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size,
			clipped: Math.max(text.right - box.right, box.left - text.left, 0),
			right: box.right,
			left: box.left,
			page: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
		};
	});
	expect(fit.visibleLabel, 'a purposeful Send label is visible').toMatch(/^Send (?:\d+|99\+)(?: comments?)?$/);
	expect(fit.lines, 'the full accessible label is one line').toBe(1);
	expect(fit.clipped, 'the full accessible label is not clipped').toBeLessThanOrEqual(0);
	expect(fit.left, 'inside the screen on the left').toBeGreaterThanOrEqual(0);
	expect(fit.right, 'inside the screen on the right').toBeLessThanOrEqual(width + 0.5);
	expect(fit.page, 'nothing scrolls sideways').toBe(0);
}
