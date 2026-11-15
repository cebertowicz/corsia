import { expect, test } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses, waitForThreadReply } from './support/mock';
import { commentEditor, commentEditorClosed } from './support/chatComments';

/**
 * A comment is never out of reach. Three comments on one phrase of a chat message are nested highlights; every one of them must
 * be reachable from the transcript wherever it is read: pressing the highlight opens a list of all three (a phone's sheet, or the
 * popover at 1440 with the sidebar and Workspace panel open, which leaves the canvas too narrow for the margin column), and with
 * both panes closed the margin column holds a card for each. The unread paint of a stack only colours it, so this holds whatever it draws.
 */

const QUOTE = 'selected phrase';
const NOTES = ['Stack note 1', 'Stack note 2', 'Stack note 3'];

for (const panes of ['open', 'closed', 'landscape'] as const) test(`every comment on one phrase is reachable from the transcript (panes ${panes})`, async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
	const phone = info.project.name.includes('mobile');
	test.skip(phone && panes === 'closed', 'A phone has no margin column and no panes to close');
	test.skip(!phone && panes === 'landscape', 'A phone turned sideways (852 x 393) is the touch project');
	test.setTimeout(180_000);
	const title = `Stack reach ${crypto.randomUUID()}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await page.setViewportSize(panes === 'landscape' ? { width: 852, height: 393 } : phone ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
	if (phone && panes !== 'landscape') await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await waitForWsConnected(page);
	const row = page.locator(`[data-message-id="${id}"]`);
	await expect(row).toBeVisible();
	for (const note of NOTES) {
		await row.evaluate((el) => {
			const body = el.querySelector('.long-message-body')!;
			const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
			let node = walker.nextNode() as Text | null;
			while (node && !node.data.includes('selected phrase')) node = walker.nextNode() as Text | null;
			if (!node) throw new Error('Selected text was not rendered');
			const start = node.data.indexOf('selected phrase');
			const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + 'selected phrase'.length);
			const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
		});
		await page.locator('.sel-comment').click();
		await commentEditor(page).fill(note);
		await page.getByRole('button', { name: 'Comment', exact: true }).click();
		await expect(page.getByRole('dialog', { name: 'New comment' })).toBeHidden();
		await commentEditorClosed(page);
	}
	await seedMockResponses(request, [{ content: 'Received comments' }]);
	await chatComposer(page).fill('Please address these');
	await page.getByRole('button', { name: 'Send message' }).click();
	await waitForThreadReply(request, thread, 'Received comments');
	const mark = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE }).first();
	await expect(mark).toBeVisible();
	await expect(row.locator('.cm-comment-pending')).toHaveCount(0);
	await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));

	let reached: string[] = [];
	if (phone || panes === 'open') {
		// The press on the highlight is the path here: it opens the list of every comment on the phrase.
		if (phone) await mark.tap(); else await mark.click();
		const surface = page.getByRole('dialog', { name: `${NOTES.length} comments` });
		await expect(surface).toBeVisible();
		const text = await surface.innerText();
		reached = NOTES.filter((note) => text.includes(note));
		// What is drawn: the sheet's panel, or the popover (its dialog can be taller than the card that clips it).
		const card = page.locator('[data-popover], [data-adaptive-drawer] .drawer-panel').first();
		// A phone's sheet leaves the whole pressed phrase visible, clear of the sheet, whichever way the phone is held.
		if (phone) {
			await expect.poll(async () => {
				const box = await mark.boundingBox(), sheet = await card.boundingBox();
				const view = page.viewportSize()!;
				// Clear of the card either side: above a sheet, or above or below a popover (turned sideways a phone is wide enough for one).
				return !!box && !!sheet && box.y >= 0 && box.y + box.height <= view.height && (box.y + box.height <= sheet.y || box.y >= sheet.y + sheet.height);
			}, { message: 'the pressed phrase is clear of the card' }).toBe(true);
		}
		// Every comment of the stack is a row the card brings into view, not only the first.
		const rows = surface.locator('[data-reply-comment]');
		await expect(rows).toHaveCount(NOTES.length);
		for (let i = 0; i < NOTES.length; i++) {
			await rows.nth(i).scrollIntoViewIfNeeded();
			const row = await rows.nth(i).boundingBox(), shown = await card.boundingBox();
			expect(!!row && !!shown && row.y >= shown.y && row.y + row.height <= shown.y + shown.height, `comment ${i + 1} of the stack is inside the card`).toBe(true);
		}
		// A row's Reply opens that comment alone, titled as one, with Back to the list.
		await surface.locator('[data-reply-comment]').first().click();
		await expect(page.getByRole('dialog', { name: 'Comment', exact: true })).toBeVisible();
		await page.getByRole('button', { name: 'Back to the list of comments' }).click();
		await expect(page.getByRole('dialog', { name: `${NOTES.length} comments` })).toBeVisible();
	} else {
		// Wide with no pane open: the margin column holds a card for each comment.
		await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
		await expect(page.locator('.chat-margin .chat-margin-card')).toHaveCount(NOTES.length);
		const text = await page.locator('.chat-margin').innerText();
		reached = NOTES.filter((note) => text.includes(note));
	}
	expect(reached, 'every comment of the stack is reachable').toEqual(NOTES);
});
