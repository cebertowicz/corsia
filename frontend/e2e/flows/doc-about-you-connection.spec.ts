import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage } from '../support/corsia';
import { openLiveDocument } from '../support/liveDocument';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { readMockCallCount, seedMockResponses, type MockReply } from '../support/mock';

/**
 * Suggestions keep typing across a connection drop: three agents' About you inserts are typing in an open, in-view
 * document when the page closes its live sockets. Each insert must be first drawn near 0 and never jump while in
 * view, through the Disconnected and Reconnecting states and after the reconnect.
 */
const promoted: string[] = [];
test.afterEach(async ({ request }) => { for (const id of promoted.splice(0)) await demoteAgent(request, id); });

async function openAgent(request: APIRequestContext, viewer: Page, title: string, promote: boolean) {
	const id = await createSession(request, title);
	if (promote) promoted.push(id);
	const page = await viewer.context().newPage();
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	return { id, title, page };
}

const HEADINGS = ['## Who you are', "## What you're working toward", '## How you like to work'];
const BODY = [
	'\n- Steve, goes by Steve; builds CORSIA, a layer that carries intent and context across your tools.\n- Based in Los Angeles; Pacific time.',
	'\n- Make agent work need less setup and attention as the number of agents grows.\n- Keep your data and hardware under your control.',
	'\n- Short answers first, the action second.\n- Plan heavily before executing; prove a hypothesis before building on it.',
];
const replacement = (i: number) => HEADINGS[i] + BODY[i];

/** The call's JSON input cut the way a provider streams it: the quote whole, then the replacement in three pieces. */
function pieces(input: string): string[] {
	const start = input.indexOf('"replacement":"') + '"replacement":"'.length;
	const end = input.indexOf('","client_id"');
	const value = input.slice(start, end);
	const third = Math.ceil(value.length / 3);
	return [input.slice(0, start), value.slice(0, third), value.slice(third, third * 2), value.slice(third * 2) + input.slice(end)];
}

for (const width of [1440, 393]) {
	test(`@surface:live-document about-you suggestions type through a drop at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sets its own viewport widths.');
		test.setTimeout(180_000);
		const stamp = `${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height: 960 } });
		try {
			const viewer = await context.newPage();
			await viewer.addInitScript(() => {
				const w = window as unknown as { __timeline: Array<{ at: number; s: string }>; __conn: Array<{ at: number; s: string }>; __sockets: WebSocket[]; __started: number; __dropAt: number[] };
				w.__timeline = []; w.__conn = []; w.__sockets = []; w.__dropAt = []; w.__started = Date.now();
				const Native = window.WebSocket;
				const Wrapped = function (this: unknown, ...args: ConstructorParameters<typeof WebSocket>) {
					const ws = new Native(...args);
					if (new URL(String(args[0]), location.href).pathname === '/ws') w.__sockets.push(ws);
					return ws;
				} as unknown as typeof WebSocket;
				Wrapped.prototype = Native.prototype;
				Object.assign(Wrapped, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
				window.WebSocket = Wrapped;
				let last = '', lastLine = '', dropped = false;
				const read = () => {
					const parts = [...document.querySelectorAll('.cm-content .cm-suggestion-insert')].map((el) =>
						`${el.getAttribute('data-suggestion-id') ? 'C:' + el.getAttribute('data-suggestion-id')!.slice(0, 4) : 'P'}=${el.getAttribute('data-insert')?.length ?? 0}`);
					const s = parts.join(' ');
					if (s !== last) { w.__timeline.push({ at: Date.now() - w.__started, s }); last = s; }
					// The viewer's own status line is the connection timeline; no line means opening or connected.
					const line = [...document.querySelectorAll('.live-line')].map((el) => el.textContent?.trim().slice(0, 24)).join('|') || 'ok';
					if (line !== lastLine) { w.__conn.push({ at: Date.now() - w.__started, s: line }); lastLine = line; }
					if (!dropped && parts.some((p) => { const n = Number(p.split('=')[1]); return n >= 25 && n < 120; })) {
						dropped = true;
						w.__dropAt.push(Date.now() - w.__started);
						for (const ws of w.__sockets) if (ws.readyState === 1) ws.close();
					}
					requestAnimationFrame(read);
				};
				requestAnimationFrame(read);
			});
			const owner = await openAgent(request, viewer, `AC Owner ${stamp}`, true);
			const live = await agentCreatesSharedDocument(request, owner, `about-you-c-${stamp}`, `# About you\n\n${HEADINGS.join('\n\n')}\n`);
			const authors = [];
			for (let i = 0; i < 3; i++) authors.push(await openAgent(request, viewer, `AC Author${i} ${stamp}`, false));
			const suggestion = (i: number): MockReply => {
				const args = { artifact_id: live.artifactId, quote: HEADINGS[i], replacement: replacement(i), client_id: `ac-${i}-${stamp}` };
				return { tool_call_stream: { id: `sg-${i}-${stamp}`, name: 'doc_comment_create', pieces: pieces(JSON.stringify(args)), delay_ms: 350 }, finish_reason: 'tool_calls' };
			};
			await seedMockResponses(request, authors.flatMap((a, i) => [
				{ tool_calls: [{ id: `read-${i}-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: a.id },
				{ content: 'Read complete.', finish_reason: 'stop', match_thread: a.id },
				{ ...suggestion(i), match_thread: a.id },
				{ content: 'Suggested.', finish_reason: 'stop', match_thread: a.id },
			]));
			for (const a of authors) {
				const before = await readMockCallCount(request);
				await sendChatMessage(a.page, 'Read this document.');
				await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(before + 2);
			}
			await openLiveDocument(viewer, live);
			await Promise.all(authors.map((a) => sendChatMessage(a.page, 'Suggest your heading.')));
			await expect(viewer.locator('.cm-content .cm-suggestion-insert')).toHaveCount(3, { timeout: 40_000 });
			await viewer.waitForTimeout(12_000);
			const snap = await viewer.evaluate(() => {
				const w = window as unknown as { __timeline: Array<{ at: number; s: string }>; __conn: Array<{ at: number; s: string }>; __dropAt: number[] };
				return { timeline: w.__timeline, conn: w.__conn, dropAt: w.__dropAt };
			});
			// The drop came while the inserts were typing, and the viewer's status line showed it.
			expect(snap.dropAt).toHaveLength(1);
			expect(snap.conn.map((c) => c.s.split(' ')[0])).toEqual(expect.arrayContaining(['Disconnected', 'Reconnecting…']));
			// Per insert: first drawn near 0, never a jump while in view, and it ends at its full length.
			const lengths = new Map<string, number[]>();
			for (const { s } of snap.timeline) {
				for (const part of s.split(' ').filter(Boolean)) {
					const [id, n] = part.split('=');
					lengths.set(id, [...(lengths.get(id) ?? []), Number(n)]);
				}
			}
			expect([...lengths.keys()]).toHaveLength(3);
			const full = HEADINGS.map((_, i) => replacement(i).length);
			for (const series of lengths.values()) {
				expect(series[0]).toBeLessThan(20);
				expect(Math.max(...series.slice(1).map((n, i) => n - series[i]))).toBeLessThanOrEqual(40);
				expect(full).toContain(series[series.length - 1]);
			}
		} finally {
			await context.close();
		}
	});
}
