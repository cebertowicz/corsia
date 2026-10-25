import path from 'node:path';
import { expect, type APIRequestContext, type BrowserContext, type Page, type WebSocket, type WebSocketRoute } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';

export interface LiveDocument {
	artifactId: string;
	body: string;
	docPath: string;
	fileName: string;
	threadId: string;
	title: string;
}

export interface LiveFrameLog {
	opened: Map<string, number>;
	sockets: WebSocket[];
}

function watchLiveFrames(page: Page): LiveFrameLog {
	const log: LiveFrameLog = { opened: new Map(), sockets: [] };
	page.on('websocket', socket => {
		if (new URL(socket.url()).pathname !== '/ws') return;
		log.sockets.push(socket);
		socket.on('framereceived', frame => {
			try {
				const message = JSON.parse(String(frame.payload)) as { type?: string; artifact_id?: string };
				if (message.type === 'doc_opened' && message.artifact_id) log.opened.set(message.artifact_id, (log.opened.get(message.artifact_id) ?? 0) + 1);
			} catch {
				return;
			}
		});
	});
	return log;
}

export interface LiveLink {
	cut(): Promise<void>;
	restore(): void;
	refused(): number;
}

/**
 * Routes the app's live socket (`/ws`) so a test can cut it. Chromium's offline emulation leaves an open
 * WebSocket open, and the app reports Disconnected only when its socket closes. Each routed socket connects
 * to the real backend and passes every frame through unchanged; cut() closes both ends, and while cut each
 * reconnect is closed before it reaches the server. Install it before the context opens a page.
 */
export async function routeLiveSocket(context: BrowserContext): Promise<LiveLink> {
	const live: Array<{ page: WebSocketRoute; server: WebSocketRoute }> = [];
	let blocked = false;
	let refused = 0;
	await context.routeWebSocket(url => url.pathname === '/ws', async page => {
		if (blocked) {
			refused += 1;
			await page.close();
			return;
		}
		live.push({ page, server: page.connectToServer() });
	});
	return {
		async cut() {
			blocked = true;
			for (const { page, server } of live.splice(0)) {
				await server.close();
				await page.close();
			}
		},
		restore() { blocked = false; },
		refused: () => refused,
	};
}

export async function expectLiveSocketsClosed(log: LiveFrameLog): Promise<void> {
	await expect.poll(() => log.sockets.length > 0 && log.sockets.every(socket => socket.isClosed())).toBe(true);
}

export async function seedLiveDocument(request: APIRequestContext, label: string, body: string): Promise<LiveDocument> {
	const title = `live-${label}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const tree = await request.get(`/api/files/tree?token=${encodeURIComponent(TEST_TOKEN)}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find(root => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const created = await request.put(`/api/files/content?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { path: docPath, text: body, thread_id: threadId, create: true },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return { artifactId: (await created.json()).artifact.id as string, body, docPath, fileName, threadId, title };
}

export async function createComment(request: APIRequestContext, document: LiveDocument, anchorText: string, body: string, occurrence = 0): Promise<void> {
	let from = -1;
	for (let index = 0; index <= occurrence; index += 1) from = document.body.indexOf(anchorText, from + 1);
	expect(from, `setup: occurrence ${occurrence} of ${anchorText}`).toBeGreaterThanOrEqual(0);
	const created = await request.post(`/api/artifacts/${document.artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { body, anchor_text: anchorText, from_off: from, to_off: from + anchorText.length, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
}

export async function openLiveDocument(page: Page, document: LiveDocument, expectedText = document.body): Promise<LiveFrameLog> {
	const frames = watchLiveFrames(page);
	await page.addInitScript(({ document }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: {
				[document.threadId]: {
					tabs: [{ instanceId: 'live-tab', artifactId: document.artifactId, path: document.docPath, title: document.fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'live-tab', visible: true, activeSurface: 'artifact',
				},
			},
			splitWidth: null,
		}));
	}, { document });
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(document.title, { exact: true }).first().click();
	// The editor renders Markdown headings and each line separately; its DOM text is not the source file.
	for (const line of expectedText.split('\n').filter(line => line.trim())) {
		await expect(page.locator('.cm-content')).toContainText(line.replace(/^#{1,6}\s+/, ''));
	}
	await expect.poll(() => frames.opened.has(document.artifactId), { timeout: 20_000 }).toBe(true);
	return frames;
}

export async function typeAtDocumentEnd(page: Page, text: string): Promise<void> {
	const editor = page.locator('.cm-content');
	await editor.click();
	await page.keyboard.press('ControlOrMeta+End');
	await page.keyboard.type(text);
}

export async function renderedCount(page: Page, text: string): Promise<number> {
	return ((await page.locator('.cm-content').textContent()) ?? '').split(text).length - 1;
}

async function lineEnd(line: ReturnType<Page['locator']>): Promise<{ x: number; y: number }> {
	await line.scrollIntoViewIfNeeded();
	const box = await line.boundingBox();
	expect(box).not.toBeNull();
	return { x: box!.x + box!.width - 2, y: box!.y + box!.height - Math.min(4, box!.height / 2) };
}

/**
 * Deletes the first rendered line holding `text`, with its preceding line break, by Backspace over a pointer
 * selection. A collapsed caret inside commented text opens that comment's popover and moves focus into it
 * (MarkdownEditor handleSelection), so the selection starts at the end of the previous line, outside any
 * comment, and the caret after the deletion stays there.
 */
export async function deleteRenderedLine(page: Page, text: string): Promise<void> {
	const before = await renderedCount(page, text);
	expect(before).toBeGreaterThan(0);
	const line = page.locator('.cm-line').filter({ hasText: text }).first();
	const previous = line.locator('xpath=preceding-sibling::div[contains(concat(" ", normalize-space(@class), " "), " cm-line ")][1]');
	const start = await lineEnd(previous);
	const end = await lineEnd(line);
	await page.mouse.move(start.x, start.y);
	await page.mouse.down();
	await page.mouse.move(end.x, end.y, { steps: 5 });
	await page.mouse.up();
	await expect.poll(() => page.evaluate(() => document.getSelection()?.toString() ?? '')).toContain(text);
	await page.keyboard.press('Backspace');
	await expect.poll(() => renderedCount(page, text)).toBe(before - 1);
	await expect(page.locator('.cm-content')).toBeFocused();
}

export async function expectRenderedCaret(page: Page, text: string): Promise<void> {
	const editor = page.locator('.cm-content');
	await expect(editor).toContainText(text);
	// This editor uses the native contenteditable caret, without CodeMirror's drawSelection extension.
	const caret = await editor.evaluate(element => {
		const selection = document.getSelection();
		if (!selection?.isCollapsed || selection.rangeCount !== 1 || !element.contains(selection.anchorNode)) return null;
		const cursor = selection.getRangeAt(0).getBoundingClientRect();
		const editorRect = element.closest<HTMLElement>('.cm-editor')?.getBoundingClientRect();
		return editorRect ? { bottom: cursor.bottom, height: cursor.height, left: cursor.left, right: cursor.right, top: cursor.top, editorBottom: editorRect.bottom, editorTop: editorRect.top } : null;
	});
	expect(caret).not.toBeNull();
	expect(caret!.height).toBeGreaterThan(0);
	expect(caret!.top).toBeGreaterThanOrEqual(caret!.editorTop);
	expect(caret!.bottom).toBeLessThanOrEqual(caret!.editorBottom);
}

export async function serverText(request: APIRequestContext, document: LiveDocument): Promise<string> {
	const response = await request.get(`/api/files/content?token=${encodeURIComponent(TEST_TOKEN)}&path=${encodeURIComponent(document.docPath)}`);
	expect(response.ok(), await response.text()).toBeTruthy();
	return (await response.json()).text as string;
}
