import { createServer } from 'node:http';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, sendChatMessage, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { seedMockResponses, waitForThreadReply } from '../support/mock';

/**
 * #2625: while the person has the shared browser open in the pane, the agent's ref clicks and fills run inside the
 * page. A real turn (mock model, real tools, real agent-browser) drives browser_click and browser_fill on snapshot refs
 * against a page served here; the page reports each handler that ran back to this server.
 */

const PAGE = `<!doctype html><html><head><title>Pane agent refs</title></head>
<body style="margin:0;font:16px sans-serif;background:#f0f0f0">
<script>function hit(name, value) { fetch('/event?name=' + name + (value === undefined ? '' : '&value=' + encodeURIComponent(value))); }</script>
<main style="padding:24px">
<h1>Pane agent refs</h1>
<p><button onclick="hit('go');this.textContent='Clicked once'">Uncovered button</button></p>
<p><label for="name">Name</label> <input id="name" oninput="hit('fill', this.value)"></p>
<p style="margin-top:24px"><button id="covered" onclick="hit('covered')">Covered button</button></p>
<p style="margin-top:24px"><span id="slot"><button onclick="hit('replaced')">Replaced button</button></span>
<button onclick="hit('replace');const s=document.getElementById('slot');s.innerHTML=s.innerHTML">Replace it</button></p>
</main>
<div id="overlay" style="position:absolute;background:rgba(200,0,0,0.35)"></div>
<script>
const r = document.getElementById('covered').getBoundingClientRect();
Object.assign(document.getElementById('overlay').style, { left: (r.left - 6 + scrollX) + 'px', top: (r.top - 6 + scrollY) + 'px', width: (r.width + 12) + 'px', height: (r.height + 12) + 'px' });
</script>
</body></html>`;

async function startRefsPage() {
	const events: string[] = [];
	const server = createServer((request, response) => {
		const url = new URL(request.url ?? '/', 'http://refs.test');
		if (url.pathname === '/event') {
			const value = url.searchParams.get('value');
			events.push(value === null ? url.searchParams.get('name') ?? '' : `${url.searchParams.get('name')}=${value}`);
			response.statusCode = 204;
			response.end();
			return;
		}
		response.setHeader('content-type', 'text/html; charset=utf-8');
		response.end(PAGE);
	});
	await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
	const { port } = server.address() as { port: number };
	return {
		url: `http://127.0.0.1:${port}/agent-refs.html`,
		events: () => [...events],
		close: () => {
			server.closeAllConnections();
			return new Promise<void>((resolve) => server.close(() => resolve()));
		},
	};
}

type Call = { name: string; arguments: Record<string, unknown> };
let turnCount = 0;

/** One real turn on `thread`: the mock model asks for each call in order, the real tools run them; returns their results. */
async function agentTurn(page: Page, request: APIRequestContext, thread: string, calls: Call[], ask: string): Promise<string[]> {
	const turn = `t${++turnCount}-${Date.now()}`;
	const ids = calls.map((_call, index) => `${turn}-c${index}`);
	await seedMockResponses(request, [
		...calls.map((call, index) => ({ tool_calls: [{ id: ids[index], name: call.name, arguments: JSON.stringify(call.arguments) }], finish_reason: 'tool_calls', match_thread: thread })),
		{ content: `done ${turn}`, finish_reason: 'stop', match_thread: thread },
	]);
	await sendChatMessage(page, ask);
	await waitForThreadReply(request, thread, `done ${turn}`, { timeout: 90_000 });
	const listing = await request.get(`/api/threads/${thread}/messages?token=${TEST_TOKEN}`);
	expect(listing.ok()).toBeTruthy();
	const body = await listing.json();
	const rows = (body.messages ?? []) as Array<{ id: string; role: string; metadata?: { tool_calls?: Array<{ id?: string }> } }>;
	return Promise.all(ids.map(async (id) => {
		const owner = rows.find((row) => row.role === 'assistant' && row.metadata?.tool_calls?.some((call) => call.id === id));
		expect(owner, `no assistant step carries tool call ${id}: ${JSON.stringify(rows).slice(0, 2000)}`).toBeTruthy();
		const result = await request.get(`/api/threads/${thread}/messages/${owner!.id}/tool-results/${id}?token=${TEST_TOKEN}`);
		expect(result.ok(), `tool result ${id}: ${result.status()}`).toBeTruthy();
		return String((await result.json()).result);
	}));
}

function ref(snapshot: string, name: string): string {
	const line = snapshot.split('\n').find((entry) => entry.includes(`"${name}"`) && /ref=e\d+/.test(entry));
	const found = line ? /ref=(e\d+)/.exec(line)?.[1] : undefined;
	expect(found, `no ref for "${name}" in snapshot:\n${snapshot.slice(0, 3000)}`).toBeTruthy();
	return `@${found}`;
}

const REFUSED = "this one couldn't";
const COVERED = 'another element covers it';
const STALE = 'the page may have changed since your snapshot';

test.describe('@flow:browser-pane agent refs while the person has the pane open', () => {
	test('uncovered refs act, a covered one is refused, a stale one is refused as stale', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop proof');
		test.setTimeout(300_000);
		const refsPage = await startRefsPage();
		try {
			const title = `pane-agent-refs-${Date.now()}`;
			const thread = await createSession(request, title);
			await loginByUrl(page);
			await sidebarThread(page, title).click();

			// The person opens the shared browser in the pane and loads the page.
			await page.getByRole('button', { name: 'Open browser', exact: true }).click();
			const address = page.getByRole('textbox', { name: 'Browser address' });
			await expect(address).toBeVisible();
			await address.fill(refsPage.url);
			const opened = page.waitForResponse((response) => response.url().includes(`/api/browser/${thread}/open`));
			await address.press('Enter');
			const openResponse = await opened;
			expect(openResponse.status(), await openResponse.text()).toBe(200);
			const canvas = page.locator('canvas[aria-label="Shared browser"]');
			await expect.poll(() => canvas.evaluate((node) => (node as HTMLCanvasElement).width > 0), { timeout: 30_000 }).toBe(true);
			await saveEvidenceScreenshot(page, testInfo, 'agent-refs-pane-open', { fullPage: false });

			const [snapshot] = await agentTurn(page, request, thread, [{ name: 'browser_snapshot', arguments: { interactive_only: true } }], 'Look at the page.');
			const refs = {
				go: ref(snapshot, 'Uncovered button'), name: ref(snapshot, 'Name'), covered: ref(snapshot, 'Covered button'),
				replaced: ref(snapshot, 'Replaced button'), replace: ref(snapshot, 'Replace it'),
			};

			// (1) A click and a fill on uncovered elements run in the page.
			const [click, fill] = await agentTurn(page, request, thread, [
				{ name: 'browser_click', arguments: { selector: refs.go } },
				{ name: 'browser_fill', arguments: { selector: refs.name, text: 'Pane proof' } },
			], 'Click the button and fill the name.');
			expect(click).not.toContain(REFUSED);
			expect(fill).not.toContain(REFUSED);
			await expect.poll(() => refsPage.events()).toEqual(expect.arrayContaining(['go', 'fill=Pane proof']));
			await saveEvidenceScreenshot(page, testInfo, 'agent-refs-uncovered', { fullPage: false });

			// (2) A covered element is refused, and its handler never runs.
			const [covered] = await agentTurn(page, request, thread, [
				{ name: 'browser_click', arguments: { selector: refs.covered } },
			], 'Click the covered button.');
			expect(covered).toContain(REFUSED);
			expect(covered).toContain(COVERED);

			// (3) A ref whose node the page re-rendered since the snapshot is refused as stale, not as covered.
			const [replace, stale] = await agentTurn(page, request, thread, [
				{ name: 'browser_click', arguments: { selector: refs.replace } },
				{ name: 'browser_click', arguments: { selector: refs.replaced } },
			], 'Re-render, then click the old ref.');
			expect(replace).not.toContain(REFUSED);
			await expect.poll(() => refsPage.events()).toContain('replace');
			expect(stale).toContain(REFUSED);
			expect(stale).toContain(STALE);
			expect(stale).not.toContain(COVERED);
			await saveEvidenceScreenshot(page, testInfo, 'agent-refs-refused', { fullPage: false });

			// Every write answered before its turn ended, so no refused handler can still arrive.
			expect(refsPage.events()).not.toContain('covered');
			expect(refsPage.events()).not.toContain('replaced');
		} finally {
			await refsPage.close();
		}
	});
});
