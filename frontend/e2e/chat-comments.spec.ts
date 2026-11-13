import path from 'node:path';
import { expect, test, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sendButton, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentEditorClosed, commentOnMessage } from './support/chatComments';

const text = 'First selected phrase. Second selected phrase. Third selected phrase.';
const quotes = ['First selected phrase', 'Second selected phrase', 'Third selected phrase'];
const notes = ['First note', 'Second note', 'Third note'];
const frames = process.env.CHAT_COMMENT_FRAMES ?? path.resolve('test-results/chat-comments');

async function select(page: Page, id: string, quote: string) {
	await page.evaluate(({ id, quote }) => {
		const row = document.querySelector<HTMLElement>(`[data-message-id="${id}"]`)!;
		row.scrollIntoView({ block: 'center' });
		const body = row.querySelector<HTMLElement>('.long-message-body')!;
		const start = body.textContent!.indexOf(quote);
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		const range = document.createRange();
		let position = 0;
		let found = false;
		for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
			if (!found && start < position + node.length) { range.setStart(node, start - position); found = true; }
			if (found && start + quote.length <= position + node.length) { range.setEnd(node, start + quote.length - position); break; }
			position += node.length;
		}
		const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
	}, { id, quote });
	await expect(page.locator('.sel-comment')).toBeVisible();
}

test.use({ video: 'off' });

for (const size of [1440, 393]) for (const scheme of ['light', 'dark'] as const) {
	test.describe(`chat comments ${size} ${scheme}`, () => {
		test.describe.configure({ timeout: 120_000 });
		test.skip(() => test.info().project.name === 'chromium-mobile', 'viewport set by this spec');
		test.use({ seedPerTest: true, viewport: { width: size, height: size === 1440 ? 900 : 852 }, colorScheme: scheme, hasTouch: size === 393, isMobile: size === 393, deviceScaleFactor: 2 });
		test('stored comments stay out of the chat bubble', async ({ page, request }) => {
			test.setTimeout(120_000);
			const title = `Stored ${size} ${scheme}`;
			const thread = await createSession(request, title);
			const quote = 'A long quoted sentence with source context. '.repeat(30);
			const note = 'A long note with a question about this comment. '.repeat(30);
			await injectMessage(request, thread, { role: 'user', content: 'A message', metadata: { reply_context: { comments: [{ id: 'c1', seq: 1, from_off: 0, to_off: quote.length, quote, anchor_prefix: '', anchor_suffix: '', note, cid: 'c1' }] } } }, TEST_TOKEN);
			await openAppPastLanding(page);
			if (size === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click();
			await waitForWsConnected(page);
			// A comment lives in its thread on the highlighted text, never in a chat bubble: the bubble shows only its own words.
			const bubble = page.locator('.messages .message.user').filter({ hasText: 'A message' });
			await expect(bubble).toBeVisible();
			await expect(bubble.locator('.sent-comment')).toHaveCount(0);
			await expect(bubble).not.toContainText('A long quoted sentence');
			await expect(bubble).not.toContainText('A long note with a question');
			await page.screenshot({ path: path.join(frames, `stored-${size}-${scheme}.png`) });
		});
		test('select, post and send', async ({ page, request }) => {
			test.setTimeout(120_000);
			const title = `Comments ${size} ${scheme}`;
			const thread = await createSession(request, title);
			const id = await injectMessage(request, thread, { role: 'assistant', content: text, metadata: { for_user: true } }, TEST_TOKEN);
			await seedMockResponses(request, [...notes, 'typed'].map(note => ({ content: `Received ${note}` })));
			await openAppPastLanding(page);
			await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
			if (size === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click();
			await waitForWsConnected(page);
			const row = page.locator(`[data-message-id="${id}"]`);
			await expect(row).toBeVisible();
			// Each comment posts from its own box as it is written: no chip in the chat input, no chat message, and it stays on its highlight.
			for (let index = 0; index < quotes.length; index++) {
				await select(page, id, quotes[index]);
				await page.locator('.sel-comment').click();
				await commentEditor(page).fill(notes[index]);
				await commentEditor(page).press('Control+Enter');
				await commentEditorClosed(page);
				await commentOnMessage(row, quotes[index]);
				await expect(page.locator('.comment-chip')).toHaveCount(0);
				await expect(page.locator('.messages .message.user')).toHaveCount(0);
			}
			await page.screenshot({ path: path.join(frames, `posted-${size}-${scheme}.png`) });
			// Typed text is still a chat message of its own, and carries no comment.
			await chatComposer(page).fill('Please address these comments');
			// On a phone Enter is a newline in the composer, so the send control is what sends there.
			if (size === 393) await sendButton(page).click();
			else await chatComposer(page).press('Enter');
			const bubble = page.locator('.messages .message.user');
			await expect(bubble).toHaveCount(1);
			await expect(bubble).toContainText('Please address these comments');
			await expect(bubble.locator('.sent-comment')).toHaveCount(0);
			await page.screenshot({ path: path.join(frames, `sent-${size}-${scheme}.png`) });
		});
	});
}
