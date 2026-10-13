import { expect, type APIRequestContext, type BrowserContext, type Locator, type Page } from '@playwright/test';
import { loginByUrl, TEST_TOKEN } from './corsia';
import { markTexts } from './liveConcurrency';
import { type LiveDocument } from './liveDocument';

export type ProofReserve = {
	client_id: string; from_off: number; to_off: number; vector: string; digest: string; text_length_utf16: number;
};
/** `reserve` rides the browser's doc_positions ask; `reserved` rides the server's answer to it. */
export type ProofFrame = {
	type: string; artifact_id?: string; store_id?: string; request_id?: string; seq?: number;
	reserve?: ProofReserve; reserved?: boolean;
};
export type CreatePayload = {
	body: string; anchor_text: string; from_off: number; to_off: number; client_id: string;
	live_capture?: boolean;
};
export type StoredComment = { id: string; body: string; anchor_text: string; from_off: number; to_off: number; orphaned?: boolean };

/** Transparent real socket tap. Unlike the existing seq-only hold, records request identity too.
 * A hold queues the real answer and subsequent server frames byte for byte; never invents an answer. */
export async function commentWire(context: BrowserContext) {
	const asks: ProofFrame[] = [], answers: ProofFrame[] = [];
	let armed = false, held: ProofFrame | null = null, flush: (() => void) | null = null;
	const parse = (payload: string | Buffer): ProofFrame | null => {
		try { return JSON.parse(String(payload)) as ProofFrame; } catch { return null; }
	};
	await context.routeWebSocket(url => url.pathname === '/ws', socket => {
		const server = socket.connectToServer();
		let queue: Array<string | Buffer> | null = null;
		socket.onMessage(payload => {
			const frame = parse(payload);
			if (frame?.type === 'doc_positions') asks.push(frame);
			server.send(payload);
		});
		server.onMessage(payload => {
			const frame = parse(payload);
			if (frame?.type === 'doc_positions') answers.push(frame);
			if (queue) { queue.push(payload); return; }
			if (armed && frame?.type === 'doc_positions') {
				armed = false; held = frame;
				const pending = [payload]; queue = pending;
				flush = () => { queue = null; for (const item of pending) socket.send(item); };
				return;
			}
			socket.send(payload);
		});
	});
	return { asks, answers, arm: () => { armed = true; held = null; }, held: () => held,
		release: () => { const deliver = flush; flush = null; deliver?.(); } };
}
export type CommentWire = Awaited<ReturnType<typeof commentWire>>;

export function observeCreates(page: Page, artifactId: string): CreatePayload[] {
	const creates: CreatePayload[] = [];
	page.on('request', outgoing => {
		if (outgoing.method() === 'POST' && new URL(outgoing.url()).pathname === `/api/artifacts/${artifactId}/comments`)
			creates.push(outgoing.postDataJSON() as CreatePayload);
	});
	return creates;
}

/** The POST consumes the reservation its capture made: it says it came from a live capture, the asks for its client id
 * carry its selection and the browser's own state, and exactly one server answer to them reserved it. Returns that ask. */
export function expectCorrelation(wire: CommentWire, payload: CreatePayload, artifactId: string): ProofReserve {
	expect(payload.live_capture, 'the POST names its capture').toBe(true);
	const asks = wire.asks.filter(frame => frame.artifact_id === artifactId && frame.reserve?.client_id === payload.client_id);
	const reserved = wire.answers.filter(answer => answer.artifact_id === artifactId && answer.reserved === true
		&& asks.some(ask => ask.request_id === answer.request_id && ask.seq === answer.seq));
	expect(reserved, 'exactly one server answer reserved the POST\'s client id').toHaveLength(1);
	const reserve = asks.find(ask => ask.request_id === reserved[0].request_id)?.reserve;
	expect(reserve, 'the answer belongs to an ask for the POST\'s selection').toMatchObject({
		client_id: payload.client_id, from_off: payload.from_off, to_off: payload.to_off });
	return reserve!;
}

export async function storedComments(request: APIRequestContext, document: LiveDocument): Promise<StoredComment[]> {
	const response = await request.get(`/api/artifacts/${document.artifactId}/comments`, { params: { token: TEST_TOKEN } });
	expect(response.ok(), await response.text()).toBeTruthy();
	return (await response.json()).comments as StoredComment[];
}

export async function expectOneComment(request: APIRequestContext, document: LiveDocument, pages: Page[], quote: string, body: string, from: number, clientId: string): Promise<void> {
	await expect.poll(() => storedComments(request, document)).toEqual([
		expect.objectContaining({ id: clientId, anchor_text: quote, body, from_off: from, to_off: from + quote.length }),
	]);
	expect((await storedComments(request, document)).every(comment => comment.orphaned !== true)).toBe(true);
	for (const page of pages) {
		await expect.poll(() => markTexts(page), { timeout: 20_000 }).toEqual([quote]);
		await expect(page.getByText('Couldn’t find its place', { exact: true })).toHaveCount(0);
	}
}

async function uncoverDocumentSurface(page: Page): Promise<void> {
	const overlay = page.locator('aside.pane.overlay:not(.collapsed)');
	if (await overlay.count()) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).press('Enter');
		await expect(overlay).toHaveCount(0);
	}
}

export async function selectLine(page: Page, quote: string): Promise<void> {
	await uncoverDocumentSurface(page);
	const line = page.locator('.cm-line').filter({ hasText: quote });
	await expect(line).toHaveCount(1);
	await line.click();
	await page.keyboard.press('Home');
	await page.keyboard.press('Shift+End');
	await expect.poll(() => page.evaluate(() => document.getSelection()?.toString() ?? '')).toBe(quote);
	await expect(page.locator('.sel-comment')).toBeVisible();
}
export async function beginComment(page: Page, quote: string, body?: string): Promise<void> {
	await selectLine(page, quote);
	await page.locator('.sel-comment').click();
	await expect(page.getByPlaceholder('Comment…')).toBeVisible();
	if (body !== undefined) await page.getByPlaceholder('Comment…').fill(body);
}
export function submitButton(page: Page): Locator { return page.locator('.composer').getByRole('button', { name: 'Comment', exact: true }); }
export async function insertPrefix(page: Page, prefix: string): Promise<void> {
	await uncoverDocumentSurface(page);
	// .cm-content carries padding-bottom: 40vh, so the default click target is the centre of
	// that box, hundreds of pixels below the last line: the caret lands at document end and the
	// prefix is appended after the whole body, which no ordering assertion can survive. Click the
	// left edge of the first body line instead, keep the shortcut as intent, and prove the caret
	// is at 0 through the selection the editor publishes before typing anything.
	await page.locator('.cm-line').first().click({ position: { x: 1, y: 2 } });
	await page.keyboard.press('ControlOrMeta+Home');
	await expect
		.poll(() => caretAtDocumentStart(page), {
			message: 'Caret never reached document position 0, so a prefix inserted here cannot precede the selection.',
		})
		.toBe(true);
	await page.keyboard.insertText(prefix);
}

/** Document position 0 in the editor's own coordinates. The editor is a contenteditable, so the
 * position a reader would see is published as the native collapsed selection: its rect is compared
 * against a DOM Range over the first non-empty text node of .cm-content, and a caret at any other
 * position does not produce that left edge. The CodeMirror cursor layer is deliberately NOT used:
 * a probe of this app at 1280 found no cursor element anywhere under the editor while the native
 * selection sat exactly on the first glyph (caret left 755, top 241, height 18 against glyph left
 * 755, top 241, height 18), so a cursor-layer lookup could only ever report false. Reads no
 * CodeMirror instance. */
async function caretAtDocumentStart(page: Page): Promise<boolean> {
	return page.evaluate(() => {
		const root = document.querySelector('.cm-content');
		if (!root) return false;
		const selection = document.getSelection();
		if (!selection || !selection.rangeCount) return false;
		const caret = selection.getRangeAt(0);
		if (!caret.collapsed || !root.contains(caret.startContainer)) return false;
		const caretRect = caret.getClientRects()[0] ?? caret.getBoundingClientRect();
		if (!caretRect.width && !caretRect.height) return false;
		let first: Text | null = null;
		const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
		while (!first && walker.nextNode()) {
			const node = walker.currentNode as Text;
			if (node.data.length) first = node;
		}
		if (!first) return false;
		const measure = document.createRange();
		measure.setStart(first, 0);
		measure.setEnd(first, 1);
		const glyph = measure.getClientRects()[0];
		if (!glyph) return false;
		const within = (a: number, b: number, tolerance: number) => Math.abs(a - b) <= tolerance;
		return within(caretRect.left, glyph.left, 2) && within(caretRect.top, glyph.top, 3);
	});
}
export async function outsideClose(page: Page): Promise<void> {
	const backdrop = page.locator('.drawer-backdrop');
	if (await backdrop.count()) {
		// Narrow layout: click the exposed corner of the real drawer backdrop, away from the comment surface.
		await backdrop.click({ position: { x: 2, y: 2 } });
	} else {
		// Wide layout: the composer is a Popover without a backdrop that closes on any pointerdown outside
		// its surface. Click a point whose topmost element is inert (no control, link, field or editable
		// ancestor) and sits outside the popover and the editor, so the caret never moves into the quote.
		const point = await page.evaluate(() => {
			const inert = (element: Element | null) => !!element && !element.closest(
				'[data-popover], .cm-editor, button, a, input, textarea, select, summary, label, [role], [tabindex], [contenteditable]');
			for (let y = 4; y < window.innerHeight; y += 12)
				for (let x = 4; x < window.innerWidth; x += 12)
					if (inert(document.elementFromPoint(x, y))) return { x, y };
			return null;
		});
		expect(point, 'No inert point outside the popover and editor to click').not.toBeNull();
		await page.mouse.click(point!.x, point!.y);
	}
	await expect(page.getByPlaceholder('Comment…')).toHaveCount(0);
}

export async function expectToastLayout(page: Page, toast: Locator): Promise<void> {
	await expect(toast).toBeVisible();
	// Measure the settled toast: mid toast-in its transformed quads carry float error (44px reads 43.99997).
	await toast.evaluate(element => Promise.all(element.getAnimations({ subtree: true }).map(animation => animation.finished)));
	const box = await toast.boundingBox(), viewport = page.viewportSize()!;
	expect(box).not.toBeNull();
	expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.y).toBeGreaterThanOrEqual(0);
	expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
	expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
	const close = toast.getByRole('button', { name: 'Dismiss', exact: true });
	const target = await close.boundingBox();
	expect(target).not.toBeNull();
	const minimum = viewport.width <= 600 ? 44 : 32;
	expect(target!.width).toBeGreaterThanOrEqual(minimum); expect(target!.height).toBeGreaterThanOrEqual(minimum);
	expect(target!.x).toBeGreaterThanOrEqual(box!.x);
	expect(target!.x + target!.width).toBeLessThanOrEqual(box!.x + box!.width);
	expect(target!.y).toBeGreaterThanOrEqual(box!.y);
	expect(target!.y + target!.height).toBeLessThanOrEqual(box!.y + box!.height);
	await toast.hover(); // Pointer pause survives the keyboard move to the submit button.
	await close.focus(); // Success, rather than expiry, must dismiss this paused toast.
	await expect(close).toBeFocused();
}

/** Initial artifact workspace only, following openLiveDocument/doc-send-on-close setup.
 * No session state or editor/store internals. Both tabs subsequently navigate through actual UI. */
export async function openTwoDocuments(page: Page, first: LiveDocument, second: LiveDocument): Promise<void> {
	await page.addInitScript(({ first, second }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [first.threadId]: { tabs: [first, second].map((doc, index) => ({
				instanceId: `proof-${index}`, artifactId: doc.artifactId, path: doc.docPath, title: doc.fileName,
				loadedRevision: null, latestRevision: null, status: 'loading',
			})), activeTabId: 'proof-0', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { first, second });
	await loginByUrl(page, { waitForComposer: false });
	if (page.viewportSize()!.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(first.title, { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toContainText(first.body.trim());
}
export async function switchDocument(page: Page, document: LiveDocument, waitForText = true): Promise<void> {
	if (page.viewportSize()!.width <= 768) {
		await page.getByRole('button', { name: /^Tabs \(/ }).click();
		await page.getByRole('dialog', { name: 'Open side panel items' }).getByRole('button', { name: document.fileName, exact: true }).click();
	} else await page.getByRole('tab', { name: document.fileName }).click();
	if (waitForText) await expect(page.locator('.cm-content')).toContainText(document.body.trim());
}
