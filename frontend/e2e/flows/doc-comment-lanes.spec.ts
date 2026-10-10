import { expect, test, type APIRequestContext, type Browser, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage } from '../support/corsia';
import { openLiveDocument } from '../support/liveDocument';
import { layouts } from '../support/liveConcurrency';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { seedMockResponses } from '../support/mock';

/**
 * An agent comment is posted, not typed: its anchor highlights in the agent colour, the comment stays out of
 * the document's comments, then the whole comment appears at once after its select sweep and hold, which never
 * exceed 5.7 seconds together. Every comment here is a real mock-provider doc_comment_create turn; nothing is injected.
 */

/** The longest arrival: a 700 ms select sweep, then a 5 s hold. */
const POST_CAP_MS = 5_700;
const SLACK_MS = 2_500;
const promoted: string[] = [];

test.afterEach(async ({ request }) => {
	for (const id of promoted.splice(0)) await demoteAgent(request, id);
});

async function openAgent(request: APIRequestContext, viewer: Page, title: string, promote: boolean) {
	const id = await createSession(request, title);
	if (promote) promoted.push(id);
	const page = await viewer.context().newPage();
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	return { id, title, page };
}

/** The viewer has the owner document open; the reviewer reads it, then comments on the closing line with `body`. */
async function reviewerComments(browser: Browser, request: APIRequestContext, width: number, height: number, label: string, body: string) {
	const stamp = `${width}-${Date.now()}`;
	const quote = `closing line ${stamp}`;
	const context = await browser.newContext({ viewport: { width, height } });
	const viewer = await context.newPage();
	const owner = await openAgent(request, viewer, `Lane Owner ${label} ${stamp}`, true);
	const live = await agentCreatesSharedDocument(request, owner, `lanes-${stamp}`, `# Lanes ${stamp}\n\nlead line ${stamp}\n\n${quote}\n`);
	await openLiveDocument(viewer, live);
	const reviewer = await openAgent(request, viewer, `Lane Reviewer ${label} ${stamp}`, false);
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls' },
		{ content: 'Read complete.', finish_reason: 'stop' },
	]);
	await sendChatMessage(reviewer.page, 'Read this document.');
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `comment-${stamp}`, name: 'doc_comment_create', arguments: JSON.stringify({ artifact_id: live.artifactId, quote, body }) }], finish_reason: 'tool_calls' },
		// The owner wake and the reviewer post-tool reply race on one mock FIFO; both say the same harmless thing.
		{ content: 'Review recorded.', finish_reason: 'stop' },
		{ content: 'Review recorded.', finish_reason: 'stop' },
	]);
	await sendChatMessage(reviewer.page, 'Comment on the closing line.');
	// The editor's select step draws the pending anchor; a click inside it ends the step and shows the comment.
	const anchor = viewer.locator(`.cm-presence-select[data-thread-id="${reviewer.id}"]`);
	return { context, viewer, reviewer, owner, anchor };
}

for (const [width, height, project] of layouts) {
	test(`@surface:live-document an agent comment highlights its anchor, stays out of the comments, then appears whole within six seconds at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const { context, viewer, reviewer, owner, anchor } = await reviewerComments(browser, request, width, height, 'wait', 'Tighten this closing line.');
		try {
			await expect(anchor).toBeVisible({ timeout: 20_000 });
			const highlighted = Date.now();
			await expect(viewer.locator(`.cm-presence-cursor[data-thread-id="${reviewer.id}"]`)).toHaveCount(1);
			// The change frame has already reached the page, yet the comment is not in the document's comments.
			await expect(viewer.getByRole('button', { name: '0 comments', exact: true })).toBeVisible();
			await viewer.screenshot({ path: testInfo.outputPath(`comment-lanes-pending-${width}.png`) });

			// The whole comment arrives at once, within the cap (plus network and render slack).
			await expect(viewer.getByRole('button', { name: '1 comment', exact: true })).toBeVisible({ timeout: POST_CAP_MS + SLACK_MS });
			expect(Date.now() - highlighted).toBeLessThanOrEqual(POST_CAP_MS + SLACK_MS);
			await expect(anchor).toHaveCount(0, { timeout: 5_000 });
			await viewer.screenshot({ path: testInfo.outputPath(`comment-lanes-posted-${width}.png`) });
			await reviewer.page.close();
			await owner.page.close();
		} finally {
			await context.close();
		}
	});
}

test('@surface:live-document clicking a pending comment anchor shows the comment at once at 1440', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop only.');
	test.setTimeout(120_000);
	// About 3,000 characters: its hold is capped, so the wait is the full sweep plus five seconds.
	const body = 'Tighten this closing line so it lands. '.repeat(75);
	const { context, viewer, anchor } = await reviewerComments(browser, request, 1440, 960, 'click', body);
	try {
		await expect(anchor).toBeVisible({ timeout: 20_000 });
		await expect(viewer.getByRole('button', { name: '0 comments', exact: true })).toBeVisible();
		const clicked = Date.now();
		await anchor.first().click();
		await expect(viewer.getByRole('button', { name: '1 comment', exact: true })).toBeVisible({ timeout: 2_000 });
		expect(Date.now() - clicked, 'shown by the click, not by the full wait').toBeLessThan(POST_CAP_MS - 1_500);
		await viewer.screenshot({ path: testInfo.outputPath('comment-lanes-clicked-1440.png') });
	} finally {
		await context.close();
	}
});
