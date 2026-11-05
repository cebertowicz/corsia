import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, routeLiveSocket } from '../support/liveDocument';
import { agentCreatesSharedDocument, demoteAgent, openAgentBeside, streamedPieces } from '../support/sharedLiveDocument';
import { readMockCallCount, seedMockResponses } from '../support/mock';

/**
 * A suggestion that lands while the open, in-view document is disconnected misses its change and activity frames. When the
 * connection returns and the comments reload finds it, it must type in from nothing like any other, never appear whole.
 */
const promoted: string[] = [];
test.afterEach(async ({ request }) => { for (const id of promoted.splice(0)) await demoteAgent(request, id); });

const HEADING = '## Who you are';
const INSERT = '\n- Steve, goes by Steve; builds CORSIA, a layer that carries intent and context across your tools.\n- Based in Los Angeles; Pacific time.';

for (const width of [1440, 393]) {
	test(`@surface:live-document about-you suggestion landing in an outage types after the reconnect at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sets its own viewport widths.');
		test.setTimeout(180_000);
		const stamp = `${Date.now()}`;
		const viewerContext = await browser.newContext({ viewport: { width, height: 960 } });
		// The agents' pages stay off the viewer's cut socket.
		const agentsContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
		try {
			const agents = await agentsContext.newPage();
			const link = await routeLiveSocket(viewerContext);
			const viewer: Page = await viewerContext.newPage();
			await viewer.addInitScript(() => {
				const w = window as unknown as { __timeline: Array<{ at: number; s: string }>; __started: number };
				w.__timeline = []; w.__started = Date.now();
				let last = '';
				const read = () => {
					const s = [...document.querySelectorAll('.cm-content .cm-suggestion-insert')]
						.map((el) => `C:${(el.getAttribute('data-suggestion-id') ?? '').slice(0, 4)}=${el.getAttribute('data-insert')?.length ?? 0}`).join(' ');
					if (s !== last) { w.__timeline.push({ at: Date.now() - w.__started, s }); last = s; }
					requestAnimationFrame(read);
				};
				requestAnimationFrame(read);
			});
			const owner = await openAgentBeside(request, agents, `AO Owner ${stamp}`, promoted);
			const live = await agentCreatesSharedDocument(request, owner, `about-you-o-${stamp}`, `# About you\n\n${HEADING}\n`);
			const author = await openAgentBeside(request, agents, `AO Author ${stamp}`);
			const args = { artifact_id: live.artifactId, quote: HEADING, replacement: HEADING + INSERT, client_id: `ao-${stamp}` };
			await seedMockResponses(request, [
				{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: author.id },
				{ content: 'Read complete.', finish_reason: 'stop', match_thread: author.id },
				{ tool_call_stream: { id: `sg-${stamp}`, name: 'doc_comment_create', pieces: streamedPieces(JSON.stringify(args)), delay_ms: 350 }, finish_reason: 'tool_calls', match_thread: author.id },
				{ content: 'Suggested.', finish_reason: 'stop', match_thread: author.id },
			]);
			const before = await readMockCallCount(request);
			await sendChatMessage(author.page, 'Read this document.');
			await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(before + 2);
			await openLiveDocument(viewer, live);
			await link.cut();
			await expect(viewer.getByText(/Disconnected/).first()).toBeVisible({ timeout: 20_000 });
			await sendChatMessage(author.page, 'Suggest your heading.');
			await expect.poll(async () => {
				const res = await request.get(`/api/artifacts/${live.artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`);
				return ((await res.json()).comments ?? []).length;
			}, { timeout: 40_000 }).toBe(1);
			// Still disconnected: the suggestion is on the server and nowhere on this canvas.
			await expect(viewer.locator('.cm-content .cm-suggestion-insert')).toHaveCount(0);
			link.restore();
			await expect(viewer.locator('.cm-content .cm-suggestion-insert')).toHaveCount(1, { timeout: 40_000 });
			await viewer.waitForTimeout(8_000);
			const timeline = await viewer.evaluate(() => (window as unknown as { __timeline: Array<{ at: number; s: string }> }).__timeline);
			const series = timeline.flatMap(({ s }) => s.split(' ').filter(Boolean).map((part) => Number(part.split('=')[1])));
			expect(series[0]).toBeLessThan(20);
			expect(Math.max(...series.slice(1).map((n, i) => n - series[i]))).toBeLessThanOrEqual(40);
			expect(series[series.length - 1]).toBe((HEADING + INSERT).length);
		} finally {
			await agentsContext.close();
			await viewerContext.close();
		}
	});
}
