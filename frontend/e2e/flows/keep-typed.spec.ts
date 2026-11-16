import { expect, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import { openCommentThread, commentEditor, commentEditorClosed, commentOnMessage, postedComments } from '../support/chatComments';
import { QUOTE } from '../support/chatCommentedMessage';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses, waitForThreadReply } from '../support/mock';
import { raiseAsk } from '../support/reviewAsk';
import { journey, open, seed } from '../support/reviewFixtures';

/**
 * keepTyped on the real composed fields at 393: a phone AutoFill suggestion that replaces a field's whole value keeps
 * what the person typed, and the app sends the kept text. A test can't make a trusted event, so the action's trust
 * seam is switched on in the page; everything else (the field, its bindings, the send) is the shipped app.
 * Real iOS WebKit is not covered here.
 */
const SUGGESTION = 'Sam Rivera';

/** The module the app itself imported, so the seam the action reads is the one switched. A string, so it is not transpiled. */
async function trustEvents(page: Page, on: boolean) {
	await page.evaluate(`import('/src/lib/actions/keepTyped.ts').then((m) => { m.eventOrigin.trusted = ${on ? '() => true' : '(event) => event.isTrusted'}; })`);
}

/** What iOS is believed to send for a tapped contact suggestion: one replacement of the field's whole value, caret at the end. */
async function autofill(field: Locator, text: string) {
	await field.evaluate((el: HTMLTextAreaElement, text: string) => {
		el.focus();
		el.setSelectionRange(el.value.length, el.value.length);
		el.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertReplacementText', data: text, bubbles: true, cancelable: true }));
		el.value = text;
		el.dispatchEvent(new InputEvent('input', { inputType: 'insertReplacementText', data: text, bubbles: true }));
	}, text);
}

/** The field itself is what a tap at its centre reaches: nothing covers it. */
async function expectUncovered(field: Locator) {
	expect(await field.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
		return !!hit && (hit === el || el.contains(hit));
	}), 'elementFromPoint at the field centre is the field').toBe(true);
}

/** Every body the page sends: HTTP request bodies and websocket frames. */
function sentBodies(page: Page): string[] {
	const sent: string[] = [];
	page.on('request', (request) => { const body = request.postData(); if (body) sent.push(body); });
	page.on('websocket', (socket) => socket.on('framesent', (frame) => { if (typeof frame.payload === 'string') sent.push(frame.payload); }));
	return sent;
}

/** The control first: with the seam off the replacement stands, so a kept value below is the action's doing. Then the repair. */
async function typeThenAutofill(page: Page, field: Locator, typed: string) {
	await field.fill(typed);
	await autofill(field, SUGGESTION);
	await expect(field, 'control: an untrusted replacement is left alone').toHaveValue(SUGGESTION);
	await field.fill(typed);
	await trustEvents(page, true);
	await autofill(field, SUGGESTION);
	await trustEvents(page, false);
	await expect(field).toHaveValue(`${typed} ${SUGGESTION}`);
	await expectUncovered(field);
}

async function selectPhrase(row: Locator) {
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
}

test('comment composer and reply keep typed text through an AutoFill replacement at 393', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-mobile', 'Requires the disposable mock instance, phone project');
	test.setTimeout(150_000);
	const sent = sentBodies(page);
	const title = `Keep typed ${crypto.randomUUID()}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await page.setViewportSize({ width: 393, height: 852 });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await waitForWsConnected(page);
	const row = page.locator(`[data-message-id="${id}"]`);
	await expect(row).toBeVisible();

	// Comment composer.
	await selectPhrase(row);
	await page.locator('.sel-comment').click();
	const composer = commentEditor(page);
	const comment = 'Please check the rollout timing';
	await typeThenAutofill(page, composer, comment);
	await page.screenshot({ path: info.outputPath('393-comment-composer-kept.png'), animations: 'disabled' });
	await seedMockResponses(request, [{ content: 'Received comment' }, { content: 'Received reply' }]);
	await page.getByRole('button', { name: 'Comment', exact: true }).click();
	await commentEditorClosed(page);
	await commentOnMessage(row, QUOTE);
	const keptComment = `${comment} ${SUGGESTION}`;
	expect(sent.some((body) => body.includes(keptComment)), 'the app sent the kept comment').toBe(true);
	await waitForThreadReply(request, thread, 'Received comment');
	await expect.poll(async () => {
		const body = await (await request.get(`/api/threads/${thread}/messages?token=${TEST_TOKEN}`)).json();
		return postedComments(body.messages).map((c) => c.note ?? c.text ?? '');
	}).toContain(keptComment);

	// Reply field.
	const drawer = await openCommentThread(row, page, QUOTE);
	const reply = drawer.getByPlaceholder('Reply…');
	const replyText = 'Also confirm the owner today';
	await typeThenAutofill(page, reply, replyText);
	await page.screenshot({ path: info.outputPath('393-reply-kept.png'), animations: 'disabled' });
	await drawer.getByRole('button', { name: 'Send reply' }).click();
	const keptReply = `${replyText} ${SUGGESTION}`;
	await expect(drawer).toContainText(keptReply);
	expect(sent.some((body) => body.includes(keptReply)), 'the app sent the kept reply').toBe(true);
	await page.screenshot({ path: info.outputPath('393-reply-sent.png'), animations: 'disabled' });
});

journey('review note keeps typed text through an AutoFill replacement at 393', [['393 light', 'phone', 'light']], async ({ page, info, mode, theme, value: doc }) => {
	// The file's backend reset (about 23 s on this host) runs inside the test's budget, ahead of the journey itself.
	test.setTimeout(120_000);
	await open(page, doc, mode, theme);
	await page.getByRole('button', { name: 'Reject', exact: true }).click();
	const note = page.getByRole('textbox', { name: /^Note for / });
	const typed = 'Please revise the second ring';
	await typeThenAutofill(page, note, typed);
	await page.screenshot({ path: info.outputPath('393-review-note-kept.png'), animations: 'disabled' });
	const clear = page.waitForRequest((request) => request.url().includes(`/api/attention/${doc.artifactId}/clear`) && request.method() === 'POST');
	await page.getByRole('button', { name: 'Reject', exact: true }).click();
	const body = (await clear).postDataJSON();
	expect(body.note, 'the review the app sent carries the kept note').toBe(`${typed} ${SUGGESTION}`);
	await expect(page.locator('.rb-receipt')).toContainText('Rejected');
	await page.screenshot({ path: info.outputPath('393-review-note-sent.png'), animations: 'disabled' });
}, async ({ request }) => {
	// The starting state is built as the journey's own fixture, outside the test's budget.
	const doc = await seed(request, 'Keep typed review note');
	raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout.');
	return doc;
});
