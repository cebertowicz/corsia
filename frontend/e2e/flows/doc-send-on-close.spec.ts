import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import {
	deliveredBatches, headerSend, heldIn, installWorkspace, leaveUnsent, openThread, seedTextDocument, expectSenders, watchSendRequests, turnOffDocumentComments,
	type TextDocument,
} from '../support/heldComments';

async function addUserComment(request: Parameters<typeof createSession>[0], artifactId: string): Promise<string> {
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { body: 'Deliver this as document feedback.', anchor_text: 'commentable', from_off: 2, to_off: 13, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
}

// Narrow viewports keep tabs in the compact sheet; its close uses the same path.
async function tabControl(page: Page, fileName: string, action: 'close' | 'select'): Promise<Locator> {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: /^Tabs \(/ }).click();
		const sheet = page.getByRole('dialog', { name: 'Open side panel items' });
		return action === 'close'
			? sheet.getByRole('button', { name: `Close ${fileName}` })
			: sheet.getByRole('button', { name: fileName, exact: true });
	}
	return action === 'close'
		? page.getByRole('button', { name: `Close ${fileName}` })
		: page.getByRole('tab', { name: fileName });
}

async function seedHeldDocument(request: Parameters<typeof createSession>[0], title: string, suffix: string, comments = 1) {
	const threadId = await createSession(request, title);
	const document: TextDocument = await seedTextDocument(request, threadId, title, suffix);
	for (let index = 0; index < comments; index++) await leaveUnsent(request, await addUserComment(request, document.artifactId));
	return { threadId, document };
}

test('@surface:doc-send-on-close sends an unsent user batch exactly once before the explicit tab close', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `send-on-close-${Date.now()}`;
	const { threadId, document } = await seedHeldDocument(request, title, 'close');
	const sends = watchSendRequests(page);
	await installWorkspace(page, threadId, [document], 0);
	await openThread(page, threadId, title);
	await turnOffDocumentComments(page);
	// The open document's header is the one sender; a held comment waits for it, with no drawer footer and no Send now.
	await expect(headerSend(page)).toHaveText('Send 1 comment');
	await expectSenders(page, ['header: Send 1 comment']);
	expect(heldIn(document.artifactId, 'document-held')).toBe(1);
	expect(sends.document, 'looking at the header sends nothing').toHaveLength(0);

	// Closing the tab sends what is still held, once, without the header Send.
	await (await tabControl(page, document.fileName, 'close')).click();
	await expect.poll(() => sends.document).toHaveLength(1);
	await expect(page.getByRole('tab', { name: document.fileName })).toHaveCount(0);
	await expect(page.getByRole('button', { name: `Close ${document.fileName}` })).toHaveCount(0);
	await expect.poll(() => heldIn(document.artifactId, 'document-held')).toBe(0);
	expect(deliveredBatches(document.artifactId, 'document-held'), 'delivered once').toBe(1);
});

test('@surface:doc-send-on-close keeps its header whole beside a long owner name and sends from it once', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `switch-away-with-a-long-owner-name-${Date.now()}`;
	const { threadId, document } = await seedHeldDocument(request, title, 'first');
	const sends = watchSendRequests(page);
	await installWorkspace(page, threadId, [document], 0);
	await openThread(page, threadId, title);
	await turnOffDocumentComments(page);
	await expect(page.getByRole('button', { name: /^1 open thread, 1 message not sent yet$/ })).toBeVisible();
	await expect(headerSend(page)).toHaveText('Send 1 comment');
	await expect(page.locator('[data-drawer-unsent]'), 'the removed drawer footer is gone').toHaveCount(0);

	// Request review shares a row with the comment count and the header Send, whatever the owner's name.
	const layout = await page.evaluate(() => {
		const row = (selector: string) => document.querySelector<HTMLElement>(`.document-header ${selector}`)!.getBoundingClientRect();
		const send = [...document.querySelectorAll<HTMLElement>('.document-header button')].find((button) => /^Send \d+ comments?$/.test(button.textContent?.trim() ?? ''))!;
		const label = document.createRange();
		label.selectNodeContents(send);
		return {
			count: row('.comment-status').top,
			review: row('.review-request').top,
			sendTop: send.getBoundingClientRect().top,
			lines: new Set([...label.getClientRects()].map((rect) => Math.round(rect.top))).size,
		};
	});
	expect(Math.abs(layout.review - layout.count), 'Request review shares a row with the comment count').toBeLessThanOrEqual(4);
	expect(layout.lines, 'the header Send reads on one line').toBe(1);

	// The header Send is the batch's own Send: one send, then it goes.
	await headerSend(page).click();
	await expect.poll(() => sends.document).toHaveLength(1);
	await expect(headerSend(page)).toHaveCount(0);
	expect(deliveredBatches(document.artifactId, 'document-held')).toBe(1);
});

// The pinned header once widened the canvas at phone width: a long Send button pushed the editor sideways.
test('@surface:doc-send-on-close at 393 px the pinned header with a pending send fits inside the canvas', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `header-fits-a-long-owner-thread-title-${Date.now()}`;
	const { threadId, document: narrow } = await seedHeldDocument(request, title, 'narrow', 2);
	await page.setViewportSize({ width: 393, height: 852 });
	await installWorkspace(page, threadId, [narrow], 0);
	await openThread(page, threadId, title);
	await turnOffDocumentComments(page);
	await expect(page.getByRole('button', { name: /^2 open threads, 2 messages not sent yet$/ })).toBeVisible();
	await expect(page.locator('.cm-content')).toBeVisible();
	await expect(headerSend(page)).toHaveText('Send 2 comments');
	await expect(headerSend(page)).toBeInViewport({ ratio: 1 });
	await expectSenders(page, ['header: Send 2 comments']);

	const layout = await page.evaluate(() => {
		const doc = document.querySelector<HTMLElement>('.doc')!;
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const bounds = doc.getBoundingClientRect();
		const outside = [...document.querySelectorAll<HTMLElement>('.document-header button')]
			.filter((button) => button.getClientRects().length > 0)
			.map((button) => ({ name: button.getAttribute('aria-label') ?? button.textContent?.trim() ?? '', rect: button.getBoundingClientRect() }))
			.filter(({ rect }) => rect.left < bounds.left - 0.5 || rect.right > bounds.right + 0.5)
			.map(({ name, rect }) => `${name}: ${Math.round(rect.left)}..${Math.round(rect.right)} outside ${Math.round(bounds.left)}..${Math.round(bounds.right)}`);
		return {
			overflow: { doc: doc.scrollWidth - doc.clientWidth, scroller: scroller.scrollWidth - scroller.clientWidth, page: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth) },
			outside,
		};
	});
	expect(layout.overflow, 'the canvas, the editor and the page have nothing to scroll sideways').toEqual({ doc: 0, scroller: 0, page: 0 });
	expect(layout.outside, 'every header button lies inside the canvas').toEqual([]);

	// A near-44 px target under a coarse pointer, inside the canvas: the small button's own box is 28 px and the design
	// system's tap area (its ::after) reaches 8 px past each edge. The header's last row sits against the document, which
	// covers the last pixel of the lower edge (43 px measured), so the hit test above and below the box must reach 42.
	const sendBox = (await headerSend(page).boundingBox())!;
	expect(sendBox.x).toBeGreaterThanOrEqual(0);
	expect(sendBox.x + sendBox.width).toBeLessThanOrEqual(393);
	if (await page.evaluate(() => matchMedia('(pointer: coarse)').matches)) {
		const reach = await headerSend(page).evaluate((button) => {
			const box = button.getBoundingClientRect();
			const x = box.left + box.width / 2;
			const hits = (y: number) => { const el = document.elementFromPoint(x, y); return !!el && button.contains(el); };
			let up = 0; while (up < 20 && hits(box.top - up - 0.5)) up++;
			let down = 0; while (down < 20 && hits(box.bottom + down + 0.5)) down++;
			return box.height + up + down;
		});
		expect(reach, 'touch target (the box and its tap area)').toBeGreaterThanOrEqual(42);
	}
});
