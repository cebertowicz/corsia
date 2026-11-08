import { test } from '../support/test';
import { expect, type APIRequestContext, type Browser, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, serverText, type LiveDocument } from '../support/liveDocument';
import { agentComment, holdRequest, markTexts, reviewerSession } from '../support/liveConcurrency';
import { observeCreates, selectLine, storedComments, submitButton } from '../support/liveCommentProof';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { readMockCallCount, readUnmatchedThreads, seedMockResponses, waitForThreadReply } from '../support/mock';

test.use({ seedPerTest: true });

/**
 * A person selects text and types a comment while an agent edits inside that text. The comment posts, anchored on
 * what remains of the selection; only a selection the agent deleted entirely asks the person to select again.
 * The agent is a real mock-provider turn calling edit_file on the shared document; nothing is injected.
 */

const LOST = 'The text you selected changed or was removed. Select the text again; your draft will carry over.';
const promoted: string[] = [];
const commentBox = (page: Page) => page.locator('.composer').filter({ has: page.getByPlaceholder('Comment, or @ to tag an agent') });

async function seedFeedback(request: APIRequestContext, threadIds: string[], content = 'Saw the comment.') {
	await seedMockResponses(request, threadIds.map(match_thread => ({ match_thread, content, finish_reason: 'stop' })));
}

async function waitForFeedback(request: APIRequestContext, threadIds: string[], content = 'Saw the comment.') {
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(threadIds.length);
	for (const threadId of threadIds) {
		await waitForThreadReply(request, threadId, content);
	}
	expect(await readUnmatchedThreads(request)).toEqual([]);
}

test.beforeEach(({}, info) => { test.skip(info.project.name === 'chromium-mobile', 'Desktop layout only: the desktop app runs WebKit at 1440.'); });
test.afterEach(async ({ request }) => {
	for (const id of promoted.splice(0)) await demoteAgent(request, id);
});

async function open(browser: Browser, request: APIRequestContext, stamp: string, quote: string) {
	const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
	const viewer = await context.newPage();
	const id = await createSession(request, `Writer ${stamp}`);
	promoted.push(id);
	const page = await context.newPage();
	await loginByUrl(page);
	await page.locator('.thread-item').filter({ hasText: `Writer ${stamp}` }).click({ timeout: 10_000 });
	const agent = { id, title: `Writer ${stamp}`, page };
	const body = `# Agent edit ${stamp}\n\nlead line ${stamp}\n\n${quote}\n\nother line ${stamp} one\n\ntail line ${stamp}\n`;
	const live = await agentCreatesSharedDocument(request, agent, `after-edit-${stamp}`, body);
	await openLiveDocument(viewer, live, `lead line ${stamp}`);
	await viewer.bringToFront();
	return { context, viewer, agent, live };
}

/** The agent reads the document, then makes one edit; resolves once the edit has left the file and the viewer's editor. */
async function agentEdits(request: APIRequestContext, viewer: Page, agent: { id: string; page: Page }, live: LiveDocument, from: string, to: string) {
	const stamp = `${Date.now()}`;
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ tool_calls: [{ id: `edit-${stamp}`, name: 'edit_file', arguments: JSON.stringify({ path: live.docPath, old_string: from, new_string: to }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ content: 'done', finish_reason: 'stop', match_thread: agent.id },
	]);
	const before = await readMockCallCount(request);
	await sendChatMessage(agent.page, 'Edit the document.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(before + 3);
	await expect.poll(async () => (await serverText(request, live)).includes(from), { timeout: 20_000 }).toBe(false);
	await viewer.bringToFront();
	await expect(viewer.locator('.cm-content')).not.toContainText(from);
}

const cases = [
	{ name: 'idle', quote: (s: string) => `target ${s} alpha beta gamma`, edit: null, anchor: (q: string) => q },
	{ name: 'edit elsewhere', quote: (s: string) => `target ${s} alpha beta gamma`, edit: (s: string) => [`other line ${s} one`, `other line ${s} two`], anchor: (q: string) => q },
	{ name: 'case change inside', quote: (s: string) => `target ${s} alpha beta gamma`, edit: () => ['alpha beta', 'ALPHA beta'], anchor: (q: string) => q.replace('alpha', 'ALPHA') },
	{ name: 'insert inside', quote: (s: string) => `target ${s} alpha beta gamma`, edit: () => ['alpha beta', 'alpha NEW beta'], anchor: (q: string) => q.replace('alpha', 'alpha NEW') },
	{ name: 'delete half', quote: (s: string) => `target ${s} alpha beta gamma`, edit: () => ['alpha beta gamma', 'alpha'], anchor: (q: string) => q.replace(' beta gamma', '') },
] as const;

for (const item of cases) {
	test(`@surface:live-document @comment-proof a comment posts anchored on the text left after: ${item.name}`, async ({ browser, request }, testInfo) => {
		test.setTimeout(180_000);
		const stamp = `${Date.now()}`, quote = item.quote(stamp), text = `comment ${item.name} ${stamp}`;
		const { context, viewer, agent, live } = await open(browser, request, stamp, quote);
		try {
			const creates = observeCreates(viewer, live.artifactId);
			await selectLine(viewer, quote);
			await viewer.locator('.sel-comment').click();
			await viewer.getByPlaceholder('Comment, or @ to tag an agent').fill(text);
			if (item.edit) { const [from, to] = item.edit(stamp); await agentEdits(request, viewer, agent, live, from, to); }
			const anchor = item.anchor(quote);
			await expect(commentBox(viewer)).not.toContainText(LOST);
			await expect(submitButton(viewer)).toBeEnabled();
			await seedFeedback(request, [agent.id]);
			await submitButton(viewer).click();
			await expect(viewer.getByPlaceholder('Comment, or @ to tag an agent')).toHaveCount(0);
			expect(creates).toHaveLength(1);
			expect(creates[0]).toMatchObject({ body: text, anchor_text: anchor });
			await expect.poll(() => storedComments(request, live)).toEqual([expect.objectContaining({ body: text, anchor_text: anchor })]);
			await expect.poll(async () => (await markTexts(viewer)).join(''), { timeout: 20_000 }).toBe(anchor);
			await waitForFeedback(request, [agent.id]);
			await viewer.screenshot({ path: testInfo.outputPath(`${item.name.replace(/ /g, '-')}-${testInfo.project.name}-1440.png`), animations: 'disabled' });
		} finally { await context.close(); }
	});
}

test('@surface:live-document @comment-proof a comment posts when the agent edits inside the selection between the click and the save', async ({ browser, request }, testInfo) => {
	test.setTimeout(180_000);
	const stamp = `${Date.now()}`, quote = `target ${stamp} alpha beta gamma`, text = `comment race ${stamp}`;
	const { context, viewer, agent, live } = await open(browser, request, stamp, quote);
	try {
		const creates = observeCreates(viewer, live.artifactId);
		const held = await holdRequest(viewer, `**/api/artifacts/${live.artifactId}/comments*`);
		await selectLine(viewer, quote);
		await viewer.locator('.sel-comment').click();
		await viewer.getByPlaceholder('Comment, or @ to tag an agent').fill(text);
		await submitButton(viewer).click();
		await held.arrived;
		await agentEdits(request, viewer, agent, live, 'alpha beta', 'ALPHA beta');
		await seedFeedback(request, [agent.id]);
		held.release();
		const anchor = quote.replace('alpha', 'ALPHA');
		await expect(viewer.getByPlaceholder('Comment, or @ to tag an agent')).toHaveCount(0);
		await expect(viewer.getByText('The document changed before your comment could be attached.')).toHaveCount(0);
		await expect.poll(() => storedComments(request, live)).toEqual([expect.objectContaining({ body: text, anchor_text: anchor })]);
		expect(creates.length).toBeGreaterThanOrEqual(1);
		await expect.poll(async () => (await markTexts(viewer)).join(''), { timeout: 20_000 }).toBe(anchor);
		await waitForFeedback(request, [agent.id]);
		await viewer.screenshot({ path: testInfo.outputPath(`race-${testInfo.project.name}-1440.png`), animations: 'disabled' });
	} finally { await context.close(); }
});

test('@surface:live-document @comment-proof deleting all of the selected text asks to select again and keeps the draft', async ({ browser, request }, testInfo) => {
	test.setTimeout(180_000);
	const stamp = `${Date.now()}`, quote = `target ${stamp} alpha beta gamma`, text = `comment delete all ${stamp}`;
	const { context, viewer, agent, live } = await open(browser, request, stamp, quote);
	try {
		const creates = observeCreates(viewer, live.artifactId);
		await selectLine(viewer, quote);
		await viewer.locator('.sel-comment').click();
		await viewer.getByPlaceholder('Comment, or @ to tag an agent').fill(text);
		await agentEdits(request, viewer, agent, live, quote, 'XYZ');
		await expect(commentBox(viewer)).toContainText(LOST);
		await expect(submitButton(viewer)).toBeDisabled();
		await expect(viewer.getByPlaceholder('Comment, or @ to tag an agent')).toHaveValue(text);
		expect(creates).toHaveLength(0);
		expect(await storedComments(request, live)).toEqual([]);
		await viewer.screenshot({ path: testInfo.outputPath(`delete-all-${testInfo.project.name}-1440.png`), animations: 'disabled' });
	} finally { await context.close(); }
});

test('@surface:live-document @comment-proof a reply on an existing thread posts', async ({ browser, request }, testInfo) => {
	test.setTimeout(180_000);
	const stamp = `${Date.now()}`, quote = `target ${stamp} alpha beta gamma`, text = `reply ${stamp}`;
	const { context, viewer, live } = await open(browser, request, stamp, quote);
	try {
		const reviewer = await reviewerSession(request, `rev-${stamp}`);
		await agentComment(request, live, reviewer.id, quote, `Existing thread ${stamp}`);
		await viewer.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
		const card = viewer.locator('.popover .ct').filter({ hasText: `Existing thread ${stamp}` });
		await expect(card).toBeVisible();
		await card.getByPlaceholder('Reply…').fill(text);
		await seedFeedback(request, [live.threadId, reviewer.id], 'Saw the reply.');
		await card.getByRole('button', { name: 'Send reply' }).click();
		await expect(card).toContainText(text);
		await expect(card.getByPlaceholder('Reply…')).toHaveValue('');
		await waitForFeedback(request, [live.threadId, reviewer.id], 'Saw the reply.');
		await viewer.screenshot({ path: testInfo.outputPath(`reply-${testInfo.project.name}-1440.png`), animations: 'disabled' });
	} finally { await context.close(); }
});
