import { expect, type APIRequestContext, type Browser, type BrowserContext, type Page } from '@playwright/test';
import { createSession, TEST_TOKEN } from './corsia';
import { openLiveDocument, type LiveDocument } from './liveDocument';

export const layouts = [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const;

export interface LivePair {
	reader: Page;
	writer: Page;
	readerContext: BrowserContext;
	close(): Promise<void>;
}

/** Two real browser contexts on one live document; `prepare` installs routes before either page opens. */
export async function openLivePair(browser: Browser, document: LiveDocument, width: number, height: number,
	prepare?: (readerContext: BrowserContext) => Promise<void>): Promise<LivePair> {
	const writerContext = await browser.newContext({ viewport: { width, height } });
	const readerContext = await browser.newContext({ viewport: { width, height } });
	await prepare?.(readerContext);
	const writer = await writerContext.newPage();
	const reader = await readerContext.newPage();
	await openLiveDocument(writer, document);
	await openLiveDocument(reader, document);
	return { reader, writer, readerContext, close: async () => { await writerContext.close(); await readerContext.close(); } };
}

/** A suggestion left by another thread, the way an agent leaves one (doc-suggestions.spec.ts seeding). */
export async function suggest(request: APIRequestContext, document: LiveDocument, reviewerId: string, quote: string, replacement: string, body: string): Promise<string> {
	const created = await request.post('/api/delegation/docs/comments', {
		params: { token: TEST_TOKEN },
		data: { artifact_id: document.artifactId, quote, replacement, body, as_session_id: reviewerId },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
}

/** A comment arriving while the document is live comes from an agent: the browser route needs the page's agreed snapshot. */
export async function agentComment(request: APIRequestContext, document: LiveDocument, reviewerId: string, quote: string, body: string): Promise<string> {
	const created = await request.post('/api/delegation/docs/comments', {
		params: { token: TEST_TOKEN },
		data: { artifact_id: document.artifactId, quote, body, as_session_id: reviewerId },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
}

export async function reviewerSession(request: APIRequestContext, stamp: string): Promise<{ id: string; title: string }> {
	const title = `Live Reviewer ${stamp}`;
	return { id: await createSession(request, title), title };
}

export async function storedCommentIds(request: APIRequestContext, document: LiveDocument): Promise<string[]> {
	const listed = await request.get(`/api/artifacts/${document.artifactId}/comments`, { params: { token: TEST_TOKEN } });
	expect(listed.ok(), await listed.text()).toBeTruthy();
	return ((await listed.json()).comments as Array<{ id: string }>).map(comment => comment.id).sort();
}

/** Every rendered comment mark's text, in document order. */
export async function markTexts(page: Page): Promise<string[]> {
	return page.locator('.cm-comment-anchor').allTextContents();
}

/** Fails the test on the placement ordering-rule report (docCommentPlacement.ts), which a clean tab must never log. */
export function watchPlacementReports(page: Page): () => string[] {
	const reports: string[] = [];
	page.on('console', message => {
		if (message.type() === 'error' && message.text().includes('Comment placement:')) reports.push(message.text());
	});
	return () => [...reports];
}

/**
 * Holds one real request until released, then sends it unchanged to the real server: a slow network,
 * not a substitute answer. The same shape as doc-suggestions.spec.ts delayDecision, kept local so that
 * accepted spec is not edited.
 */
export async function holdRequest(page: Page, pattern: string, method = 'POST') {
	let release!: () => void;
	let requested!: () => void;
	const pending = new Promise<void>(resolve => { release = resolve; });
	const arrived = new Promise<void>(resolve => { requested = resolve; });
	let held = false;
	await page.route(pattern, async route => {
		if (held || route.request().method() !== method) { await route.fallback(); return; }
		held = true;
		requested();
		await pending;
		await route.continue();
	});
	return { arrived, release };
}

export type CreateLoss = 'request' | 'response';

/**
 * Drops one comment create late, inside the client's 12 s COMMENT_POST_TIMEOUT_MS. `request`: the
 * connection fails before the server sees it. `response`: the real server commits it and the answer is
 * lost on the way back. `committed()` is the id the real server returned, and `clientId()` the one sent.
 */
export async function loseCommentCreate(page: Page, artifactId: string, loss: CreateLoss) {
	let release!: () => void;
	let requested!: () => void;
	let committed: string | null = null;
	let clientId: string | null = null;
	const pending = new Promise<void>(resolve => { release = resolve; });
	const arrived = new Promise<void>(resolve => { requested = resolve; });
	let used = false;
	const pattern = (url: URL) => url.pathname === `/api/artifacts/${artifactId}/comments`;
	await page.route(pattern, async route => {
		if (used || route.request().method() !== 'POST') { await route.fallback(); return; }
		used = true;
		clientId = (route.request().postDataJSON() as { client_id?: string }).client_id ?? null;
		requested();
		if (loss === 'response') {
			const response = await route.fetch();
			expect(response.ok(), await response.text()).toBeTruthy();
			committed = (await response.json()).comment.id as string;
		}
		await pending;
		await route.abort('connectionreset');
	});
	return { arrived, release, committed: () => committed, clientId: () => clientId };
}

export interface PositionsFrame { type?: string; store_id?: string; seq?: number; digest?: string }

function positionsFrame(payload: string | Buffer): PositionsFrame | null {
	try { return JSON.parse(String(payload)) as PositionsFrame; } catch { return null; /* Non-JSON frames pass unchanged. */ }
}

/**
 * Routes the context's live socket. Client frames pass unchanged; `asks()` lists the seq of every
 * positions ask the page sent. Once armed, the next real `doc_positions` answer and every later server
 * frame are held in order until released, then delivered byte for byte: a slow link, never a changed
 * answer. `ahead` may send extra frames before the held answer.
 */
async function holdNextPositions(context: BrowserContext, ahead?: (frame: PositionsFrame, send: (frame: string) => void) => void) {
	let armed = false;
	let held: PositionsFrame | null = null;
	let flush: (() => void) | null = null;
	const asks: number[] = [];
	await context.routeWebSocket(url => url.pathname === '/ws', socket => {
		const server = socket.connectToServer();
		let queue: Array<string | Buffer> | null = null;
		socket.onMessage(payload => {
			const frame = positionsFrame(payload);
			if (frame?.type === 'doc_positions' && typeof frame.seq === 'number') asks.push(frame.seq);
			server.send(payload);
		});
		server.onMessage(payload => {
			if (queue) { queue.push(payload); return; }
			const frame = positionsFrame(payload);
			if (armed && frame?.type === 'doc_positions' && frame.store_id) {
				armed = false;
				held = frame;
				ahead?.(frame, extra => socket.send(extra));
				const pending: Array<string | Buffer> = [payload];
				queue = pending;
				flush = () => { queue = null; for (const item of pending) socket.send(item); };
				return;
			}
			socket.send(payload);
		});
	});
	return {
		arm: () => { armed = true; },
		held: () => held,
		asks: () => [...asks],
		release: () => { const deliver = flush; flush = null; deliver?.(); },
	};
}

/** Holds the next real positions answer, unmodified, behind every later server frame until released. */
export async function holdPositionsAnswer(context: BrowserContext) {
	return holdNextPositions(context);
}

/**
 * Negative protocol fault, never server behaviour: the next real `doc_positions` answer is preceded by
 * one copy whose only change is its `store_id`. That real answer, and every later server frame, is held
 * in order until released, then delivered byte for byte.
 */
export async function injectForeignPositions(context: BrowserContext) {
	let injected = 0;
	const hold = await holdNextPositions(context, (frame, send) => {
		injected += 1;
		send(JSON.stringify({ ...frame, store_id: `foreign-${frame.store_id}` }));
	});
	return { arm: hold.arm, injected: () => injected, release: hold.release };
}

/**
 * Records every comment mark text the page renders from now on, read from the public DOM after each
 * mutation, so a mark drawn and then corrected still counts.
 */
export async function recordMarkTexts(page: Page): Promise<() => Promise<string[]>> {
	await page.evaluate(() => {
		const seen = new Set<string>();
		const root = window.document;
		const read = () => { for (const mark of root.querySelectorAll('.cm-comment-anchor')) seen.add(mark.textContent ?? ''); };
		read();
		new MutationObserver(read).observe(root.querySelector('.cm-content')!, { subtree: true, childList: true, characterData: true });
		(window as unknown as { e2eMarkTexts: Set<string> }).e2eMarkTexts = seen;
	});
	return () => page.evaluate(() => [...(window as unknown as { e2eMarkTexts: Set<string> }).e2eMarkTexts]);
}

/**
 * The text of every device copy of the document in this page's IndexedDB, current store included, read
 * by the app's own reader (deviceRecovery.ts readDeviceRecovery, whose skipped store id is '' here) on a
 * separate Y.Doc. It imports that module from the Vite dev server the e2e config runs.
 */
export async function deviceCopies(page: Page, artifactId: string): Promise<string[]> {
	return page.evaluate(async id => {
		const path = '/src/lib/stores/deviceRecovery.ts';
		const recovery = await import(/* @vite-ignore */ path) as { readDeviceRecovery(artifactId: string, storeId: string): Promise<Array<{ text: string }>> };
		return (await recovery.readDeviceRecovery(id, '')).map(copy => copy.text);
	}, artifactId);
}

/** The digest a `doc_positions` answer carries for its text (documentWire.ts textDigest). */
export async function textDigest(text: string): Promise<string> {
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
	return Array.from(new Uint8Array(digest).subarray(0, 8), byte => byte.toString(16).padStart(2, '0')).join('');
}

/** Observe the reader's actual message events; routed socket.send frames bypass network-level events. */
export async function countReceived(page: Page, marker: string): Promise<() => Promise<number>> {
	await page.evaluate(async value => {
		const path = '/src/lib/ws/state.ts';
		const { ws } = await import(/* @vite-ignore */ path) as { ws: WebSocket | null };
		if (!ws) throw new Error('The reader has no live socket to observe.');
		const received = { count: 0 };
		ws.addEventListener('message', event => { if (String(event.data).includes(value)) received.count += 1; });
		(window as unknown as { e2eReceivedFrames: { count: number } }).e2eReceivedFrames = received;
	}, marker);
	return () => page.evaluate(() => (window as unknown as { e2eReceivedFrames: { count: number } }).e2eReceivedFrames.count);
}
