/**
 * Shared driver for the agent-opens-document flows: real documents, a gated
 * mock `document_open` call, the socket facts the test synchronizes on, and
 * snapshots of the canvas a request must not disturb.
 */
import path from 'node:path';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { chatComposer, createSession, loginByUrl, sendChatMessage, TEST_TOKEN, waitForWsConnected } from './corsia';
import { readMockCallCount, releaseMockGate, seedMockResponses } from './mock';

export interface TestDocument {
	artifactId: string;
	docPath: string;
	name: string;
	body: string;
}

export interface OpenResult {
	outcome: string;
	reason?: string | null;
	link?: string;
	count?: number;
}

export interface Capability {
	canShow: boolean;
	visibleArtifactId: string | null;
}

/** Socket facts observed on the wire, whichever way the socket is watched. */
export interface SocketLog {
	claimed: Set<string>;
	capabilities: Map<string, Capability>;
	results: OpenResult[];
}

export function newSocketLog(): SocketLog {
	return { claimed: new Set(), capabilities: new Map(), results: [] };
}

function parse(payload: string | Buffer): Record<string, unknown> | null {
	try {
		const data = JSON.parse(String(payload));
		return data && typeof data === 'object' ? data : null;
	} catch {
		return null;
	}
}

/** Record a server frame: presence claim acknowledgements and tool results. */
export function recordServerFrame(log: SocketLog, payload: string | Buffer): Record<string, unknown> | null {
	const data = parse(payload);
	if (data?.type === 'thread_updates_changed' && Array.isArray(data.thread_ids)) {
		for (const threadId of data.thread_ids) log.claimed.add(String(threadId));
	}
	if (data?.type === 'tool_done' && data.name === 'document_open') {
		log.results.push(typeof data.result === 'string' ? JSON.parse(data.result) : data.result as OpenResult);
	}
	return data;
}

/** Record a client frame: the latest document-open fact published per thread. */
export function recordClientFrame(log: SocketLog, payload: string | Buffer): void {
	const data = parse(payload);
	if (data?.type !== 'presence' || data.state !== 'document_open_capability') return;
	const fact = data.document_open as { can_show: boolean; visible_artifact_id: string | null };
	log.capabilities.set(String(data.thread_id), {
		canShow: fact.can_show,
		visibleArtifactId: fact.visible_artifact_id,
	});
}

/** Observe every socket the page opens without altering its traffic. */
export function watchSocket(page: Page): SocketLog {
	const log = newSocketLog();
	page.on('websocket', (socket) => {
		socket.on('framereceived', (frame) => { recordServerFrame(log, frame.payload); });
		socket.on('framesent', (frame) => recordClientFrame(log, frame.payload));
	});
	return log;
}

/** The published fact the server will snapshot when the queued tool runs. */
export async function expectCapability(log: SocketLog, threadId: string, expected: Capability) {
	await expect.poll(() => log.capabilities.get(threadId), { timeout: 10_000 }).toEqual(expected);
}

export async function createDocument(request: APIRequestContext, threadId: string, name: string): Promise<TestDocument> {
	const token = encodeURIComponent(TEST_TOKEN);
	const tree = await request.get(`/api/files/tree?token=${token}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const docPath = path.join(workspace!, name);
	const body = `Only in ${name}.`;
	const created = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: `# ${name}\n\n${body}\n`, thread_id: threadId, create: true },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return { artifactId: (await created.json()).artifact.id as string, docPath, name, body };
}

/** The document text the server holds, read outside the page's routes. */
export async function serverText(request: APIRequestContext, doc: TestDocument): Promise<string> {
	const token = encodeURIComponent(TEST_TOKEN);
	const response = await request.get(`/api/files/content?token=${token}&path=${encodeURIComponent(doc.docPath)}`);
	expect(response.ok(), await response.text()).toBeTruthy();
	return (await response.json()).text as string;
}

/** Create a thread with two documents, open it, and wait for the presence claim. */
export async function openThreadView(page: Page, request: APIRequestContext, log: SocketLog, label: string) {
	const title = `agent-open-${label}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const a = await createDocument(request, threadId, `${title}-a.md`);
	const b = await createDocument(request, threadId, `${title}-b.md`);
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await waitForWsConnected(page);
	await chatComposer(page).click();
	await expect.poll(() => log.claimed.has(threadId), { timeout: 10_000 }).toBe(true);
	return { threadId, a, b };
}

export async function revealWorked(page: Page) {
	const worked = page.getByRole('button', { name: /Expand Worked/ }).last();
	await expect(worked).toBeVisible();
	// Leaving the composer changes compact keyboard spacing. Settle focus
	// before the pointer press so the disclosure stays under its release.
	await worked.focus();
	await worked.click();
}

/** Reveal the earliest completed turn, excluding a bare active work group. */
export async function revealEarlierCompletedWork(page: Page, link: Locator) {
	// Match both accessible labels so the locator retains the same completed
	// turn when expanding changes its label from Expand to Collapse.
	const earlier = page.getByRole('button', { name: /^(Expand|Collapse) Worked ·/ }).first();
	await expect(earlier).toBeVisible();
	// postLinks may have left this completed response open. Establish the
	// collapsed precondition before verifying one explicit expansion.
	if (await earlier.getAttribute('aria-expanded') === 'true') {
		await earlier.focus();
		await earlier.click();
	}
	await expect(earlier).toHaveAttribute('aria-expanded', 'false');
	await earlier.focus();
	await earlier.click();
	await expect(earlier).toHaveAttribute('aria-expanded', 'true');
	await expect(link).toBeVisible();
}

/** Post an assistant message carrying user-tappable document links. */
export async function postLinks(page: Page, request: APIRequestContext, content: string) {
	await seedMockResponses(request, [{ content, finish_reason: 'stop' }]);
	await sendChatMessage(page, 'Give me the document links.');
	await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(1);
	await revealWorked(page);
}

let gateSeq = 0;

/** The second document-open gate in one seeded turn. */
export function thenGate(gate: string): string {
	return `${gate}-then`;
}

function gatedOpen(doc: TestDocument, gate: string) {
	return {
		tool_calls: [{ id: gate, name: 'document_open', arguments: JSON.stringify({ artifact_id: doc.artifactId }) }],
		completion_gate: gate,
		finish_reason: 'tool_calls',
	};
}

/**
 * Send the request and hold the model's `document_open` call at its gate, so
 * the canvas can be arranged before the tool snapshots presence. With
 * `holdCompletion`, the reply after the tool is gated too. With `then`, a second
 * document-open call waits at thenGate(gate) while the canvas stays mounted.
 */
export async function queueOpen(
	page: Page, request: APIRequestContext, doc: TestDocument, completion: string,
	{ holdCompletion = false, then }: { holdCompletion?: boolean; then?: TestDocument } = {},
) {
	gateSeq += 1;
	const gate = `open-${Date.now()}-${gateSeq}`;
	const queuedInputs = page.getByText('Show the document in my canvas.', { exact: true });
	const queuedInputCount = await queuedInputs.count();
	await seedMockResponses(request, [
		gatedOpen(doc, gate),
		...(then ? [gatedOpen(then, thenGate(gate))] : []),
		{ content: completion, finish_reason: 'stop', ...(holdCompletion ? { completion_gate: `${gate}-reply` } : {}) },
	]);
	await sendChatMessage(page, 'Show the document in my canvas.');
	await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(1);
	await expect(queuedInputs).toHaveCount(queuedInputCount + 1);
	await expect(queuedInputs.last()).toBeVisible();
	await expect(page.getByRole('button', { name: /^(Expand|Collapse) current work:/ })).toBeVisible();
	return gate;
}

/** Release the held call and return exactly the result it produced. */
export async function releaseOpen(request: APIRequestContext, log: SocketLog, gate: string, calls = 2): Promise<OpenResult> {
	const before = log.results.length;
	await releaseMockGate(request, gate);
	await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(calls);
	await expect.poll(() => log.results.length, { timeout: 10_000 }).toBe(before + 1);
	return log.results[before];
}

/** A link the user can show themselves, because no screen could show it now. */
export function expectCannotShowLink(result: OpenResult) {
	expect(result).toMatchObject({ outcome: 'link', reason: 'user_cannot_show' });
}

/** The canvas, editor selection, and focus a declined request must leave alone. */
export async function canvasState(page: Page, threadId: string) {
	return page.evaluate((id) => {
		const stored = JSON.parse(localStorage.getItem('corsia:artifactWorkspace:v1') ?? '{"threads":{}}');
		const ws = stored.threads?.[id] ?? null;
		const editor = document.querySelector('.cm-content');
		const active = document.activeElement;
		const selection = window.getSelection();
		return {
			tabs: (ws?.tabs ?? null)?.map((tab: { instanceId: string; artifactId: string | null; path: string }) => ({
				instanceId: tab.instanceId, artifactId: tab.artifactId, path: tab.path,
			})) ?? null,
			activeTabId: ws?.activeTabId ?? null,
			visible: ws?.visible ?? null,
			activeSurface: ws?.activeSurface ?? null,
			editorText: editor?.textContent ?? null,
			editorFocused: !!editor && !!active && editor.contains(active),
			focusedLabel: active?.getAttribute('aria-label') ?? active?.className ?? null,
			selectionInEditor: !!editor && !!selection?.anchorNode && editor.contains(selection.anchorNode),
			anchorText: selection?.anchorNode?.textContent ?? null,
			anchor: selection?.anchorOffset ?? null,
			head: selection?.focusOffset ?? null,
		};
	}, threadId);
}

/** The artifact id of the thread's active tab, or null. */
export async function activeArtifactId(page: Page, threadId: string): Promise<string | null> {
	const state = await canvasState(page, threadId);
	return state.tabs?.find((tab: { instanceId: string }) => tab.instanceId === state.activeTabId)?.artifactId ?? null;
}

/** Click into the editor and type at the end of the document. */
export async function typeAtEnd(page: Page, text: string) {
	await page.locator('.cm-content').click();
	await page.keyboard.press('ControlOrMeta+End');
	await page.keyboard.type(text);
}
