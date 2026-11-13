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

for (const panes of ['open', 'closed'] as const) test(`every comment on one phrase is reachable from the transcript (panes ${panes})`, async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
	const phone = info.project.name.includes('mobile');
	test.skip(phone && panes === 'closed', 'A phone has no margin column and no panes to close');
	test.setTimeout(180_000);
	const title = `Stack reach ${crypto.randomUUID()}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await page.setViewportSize(phone ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
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
