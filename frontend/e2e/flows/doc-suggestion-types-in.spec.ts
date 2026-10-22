import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage } from '../support/corsia';
import { openLiveDocument } from '../support/liveDocument';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { seedMockResponses } from '../support/mock';

/**
 * An agent's suggestion types its insert in like a direct edit, then stays as a suggestion, also when its lane is
 * busy with earlier comments. The suggestion is a real mock-provider doc_comment_create turn with an explicit
 * client_id and nothing streamed ahead of it; the
 * viewer's page is only watched, never driven.
 */

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

/** About a thousand characters over several lines and bullets, as an agent writes a section. */
const INSERT = [
	'Docs with you, not for you.',
	'',
	'- Every document is a place the two of you work in at the same time.',
	'- The agent suggests a change and you see it arrive where it belongs.',
	'- Nothing lands in the file until you accept it, and nothing is lost if you do not.',
	'- Edits the agent is sure of go straight in, typed where you can watch them.',
	'',
	'What you can do here:',
	'',
	'- Read along while the agent writes, or look away and find it done.',
	'- Click any suggested change to open its card, accept it, or answer it.',
	'- Ask for another pass and the agent keeps working in the same document.',
	'- Share the document with another agent and it joins the same thread of work.',
	'',
	'Why it matters: the document is the shared surface, so what you review is the outcome, not a transcript of how it got there.',
	'Keep this section short in the final outline; the detail lives in the guide.',
].join('\n');

const VARIANTS = [
	{ name: 'an agent suggestion types its insert in, then stays a suggestion', busy: false },
	// Two comments ahead of it keep the agent's lane busy past the wait for the suggestion's own activity frame.
	{ name: 'an agent suggestion that commits while its lane is busy waits its turn, then types in', busy: true },
];

for (const variant of VARIANTS) test(`@surface:live-document ${variant.name}`, async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop only.');
	test.setTimeout(120_000);
	const stamp = `${Date.now()}`;
	const quote = `Docs with you ${stamp}`;
	const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
	try {
		const viewer = await context.newPage();
		const owner = await openAgent(request, viewer, `Types In Owner ${stamp}`, true);
		const live = await agentCreatesSharedDocument(request, owner, `types-in-${stamp}`, `# Outline ${stamp}\n\n## ${quote}\n\nlead line ${stamp}\n\nfirst point ${stamp}\n\nsecond point ${stamp}\n`);
		await openLiveDocument(viewer, live);
		const author = await openAgent(request, viewer, `Types In Author ${stamp}`, false);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls' },
			{ content: 'Read complete.', finish_reason: 'stop' },
		]);
		await sendChatMessage(author.page, 'Read this document.');

		// Record every length the drawn insert takes on, from before the suggestion exists.
		await viewer.evaluate(() => {
			const seen: Array<{ at: number; length: number }> = [];
			(window as unknown as { __insertLengths: typeof seen }).__insertLengths = seen;
			const started = Date.now();
			(window as unknown as { __started: number }).__started = started;
			// Sampled once per painted frame, so only what the viewer could see counts.
			const read = () => {
				const el = document.querySelector('.cm-content .cm-suggestion-insert');
				const length = el?.getAttribute('data-insert')?.length;
				if (length !== undefined && seen[seen.length - 1]?.length !== length) seen.push({ at: Date.now() - started, length });
				requestAnimationFrame(read);
			};
			requestAnimationFrame(read);
		});

		const comment = (quote: string) => ({ id: `comment-${quote}`, name: 'doc_comment_create', arguments: JSON.stringify({
			artifact_id: live.artifactId, quote, body: 'A long enough remark to hold the lane while it is written out. '.repeat(6),
		}) });
		await seedMockResponses(request, [
			{ tool_calls: [
				...(variant.busy ? [comment(`first point ${stamp}`), comment(`second point ${stamp}`)] : []),
				{ id: `suggest-${stamp}`, name: 'doc_comment_create', arguments: JSON.stringify({
					artifact_id: live.artifactId, quote, replacement: INSERT, client_id: `types-in-${stamp}`,
				}) },
			], finish_reason: 'tool_calls' },
			{ content: 'Suggested.', finish_reason: 'stop' },
			{ content: 'Suggested.', finish_reason: 'stop' },
		]);
		const sentAt = Date.now();
		await sendChatMessage(author.page, 'Suggest the section.');

		const insert = viewer.locator('.cm-content .cm-suggestion-insert');
		await expect(insert).toHaveCount(1, { timeout: 30_000 });
		// Timed frames of it typing in, for the review of the journey.
		for (const [index, wait] of [0, 1_200, 1_200].entries()) {
			await viewer.waitForTimeout(wait);
			await viewer.screenshot({ path: testInfo.outputPath(`typing-${variant.busy ? 'busy' : 'plain'}-${index + 1}.png`) });
		}
		await expect.poll(async () => (await insert.getAttribute('data-insert'))?.length, { timeout: 30_000 }).toBe(INSERT.length);
		const seen = await viewer.evaluate(() => (window as unknown as { __insertLengths: Array<{ at: number; length: number }> }).__insertLengths);
		const started = await viewer.evaluate(() => (window as unknown as { __started: number }).__started);
		await testInfo.attach('insert-lengths.json', { body: JSON.stringify({ sentAtMs: sentAt - started, seen }), contentType: 'application/json' });
		console.log('INSERT-LENGTHS', JSON.stringify({ sentAtMs: sentAt - started, seen: seen.slice(0, 12) }));
		// Typed in: the insert is first seen short and grows over several steps, never whole on first sight.
		expect(seen[0].length, `first drawn length ${JSON.stringify(seen.slice(0, 5))}`).toBeLessThan(INSERT.length / 2);
		expect(seen.length, 'the insert grew in steps').toBeGreaterThan(5);
		expect(seen[seen.length - 1].at - seen[0].at, 'and took a visible time').toBeGreaterThan(2_000);
		await viewer.screenshot({ path: testInfo.outputPath(`typing-${variant.busy ? 'busy' : 'plain'}-done.png`) });
	} finally {
		await context.close();
	}
});
