import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { loginByUrl, TEST_TOKEN } from './corsia';

// Held-comment delivery: a held comment waits for a person's Send. Its one sender is the surface that owns its lane:
// the document header (document-held), the image viewer bar (image-held), and the chat for the thread's document-held
// comments whose own header is not on screen.

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const HOLD_LATER = "strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 hour')";

/** The key this browser kept the preference under before the server did; the app hands an "off" over once. */
export const DELIVER_IMMEDIATELY_KEY = 'corsia.document-comments.deliver-immediately';

const DELIVERY_PREFERENCE_ROUTE = '/api/doc-feedback/delivery-preference';

/** Writes the signed-in person's "Deliver comments immediately" preference on the server, as the header toggle does. */
export async function setDeliveryPreference(request: APIRequestContext, immediate: boolean): Promise<void> {
	const saved = await request.put(DELIVERY_PREFERENCE_ROUTE, { headers: { Authorization: `Bearer ${TEST_TOKEN}` }, data: { immediate } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
}

/** What the server holds for the signed-in person: the choice, and whether they have made one. */
export async function readDeliveryPreference(request: APIRequestContext): Promise<{ immediate: boolean; stored: boolean }> {
	const read = await request.get(DELIVERY_PREFERENCE_ROUTE, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
	expect(read.ok(), await read.text()).toBeTruthy();
	return read.json();
}

/** Runs a short script against the isolated E2E database; only synthetic mock state may be touched. */
export function isolatedDb(script: string[], ...args: string[]): string {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated E2E mock state');
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', ['import json, sqlite3, sys', 'connection = sqlite3.connect(sys.argv[1])', ...script, 'connection.commit()', 'connection.close()'].join('\n'),
		path.join(stateRoot, 'corsia.db'), ...args,
	], { cwd: repoRoot, encoding: 'utf8' });
}

/**
 * The server delivers an open person batch after the person has been idle for two minutes. While the hold is on,
 * database triggers push every person batch that is created or touched an hour out, so a spec reads the held state
 * with no race against that deadline.
 */
export function holdPersonBatches(on: boolean): void {
	const touched = "NEW.author_kind='user' AND NEW.state='open' AND NEW.closes_at<strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+30 minutes')";
	const statements = on
		? [
			`CREATE TRIGGER IF NOT EXISTS e2e_hold_insert AFTER INSERT ON doc_feedback_batches WHEN NEW.author_kind='user' BEGIN UPDATE doc_feedback_batches SET closes_at=${HOLD_LATER} WHERE id=NEW.id; END`,
			`CREATE TRIGGER IF NOT EXISTS e2e_hold_touch AFTER UPDATE OF closes_at ON doc_feedback_batches WHEN ${touched} BEGIN UPDATE doc_feedback_batches SET closes_at=${HOLD_LATER} WHERE id=NEW.id; END`,
		]
		: ['DROP TRIGGER IF EXISTS e2e_hold_insert', 'DROP TRIGGER IF EXISTS e2e_hold_touch'];
	isolatedDb(['for statement in sys.argv[2:]: connection.execute(statement)'], ...statements);
}

const tok = () => encodeURIComponent(TEST_TOKEN);

/** Give the ambient CORSIA agent the mock model before a frame journey can surface its first-run wake. */
export async function setMockCorsiaAgentModel(request: APIRequestContext): Promise<void> {
	const agent = await request.get(`/api/corsia-agent?token=${tok()}`);
	expect(agent.ok(), await agent.text()).toBeTruthy();
	const threadId = (await agent.json()).thread_id as string | null;
	expect(threadId, 'mock bootstrap creates the ambient CORSIA agent').toEqual(expect.any(String));
	const selected = await request.patch(`/api/threads/${threadId}?token=${tok()}`, {
		data: { model: 'mock/test-model' },
	});
	expect(selected.ok(), await selected.text()).toBeTruthy();
}

/** Keep developer provenance visible in normal source-checkout use, but exclude it from person-facing capture evidence. */
export async function setDebugVersionOverlay(request: APIRequestContext, enabled: boolean): Promise<void> {
	const updated = await request.patch('/api/settings', {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` },
		data: { features: { debug_version_overlay: enabled } },
	});
	expect(updated.ok(), await updated.text()).toBeTruthy();
}

/** Turns the "Deliver comments immediately" preference off on the server before the app loads, so the page's own comments are held. */
export async function holdDocumentComments(request: APIRequestContext): Promise<void> {
	await setDeliveryPreference(request, false);
}

/** Set the preference through the real document header, as a person does. */
export async function turnOffDocumentComments(page: Page): Promise<void> {
	const toggle = page.getByRole('button', { name: 'Deliver comments immediately', exact: true });
	await expect(toggle).toBeVisible();
	if (await toggle.getAttribute('aria-pressed') === 'true') await toggle.click();
	await expect(toggle).toHaveAttribute('aria-pressed', 'false');
	await expect.poll(async () => (await readDeliveryPreference(page.request)).immediate).toBe(false);
}

/** Makes `count` document-held comments on a document, as a person's would be with delivery-immediately off. */
export async function seedHeldComments(
	request: APIRequestContext, artifactId: string, count: number, label = 'Held',
	anchor = { text: 'commentable', from: 2, to: 13 },
): Promise<string[]> {
	holdPersonBatches(true);
	try {
		const ids: string[] = [];
		for (let index = 1; index <= count; index++) {
			const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${tok()}`, {
				data: { body: `${label} comment ${index}`, anchor_text: anchor.text, from_off: anchor.from, to_off: anchor.to, delivery_policy: 'document-held' },
			});
			expect(created.ok(), await created.text()).toBeTruthy();
			ids.push((await created.json()).comment.id as string);
		}
		return ids;
	} finally {
		holdPersonBatches(false);
	}
}

/**
 * A re-opened comment is held for the document owner: resolve then re-open a comment to leave one document-held message
 * (a comment made through the API is delivered as it is made).
 */
export async function leaveUnsent(request: APIRequestContext, commentId: string): Promise<void> {
	holdPersonBatches(true);
	try {
		for (const resolved of [true, false]) {
			const patched = await request.patch(`/api/comments/${commentId}`, { params: { token: TEST_TOKEN }, data: { resolved, delivery_policy: 'document-held' } });
			expect(patched.ok(), await patched.text()).toBeTruthy();
		}
	} finally {
		holdPersonBatches(false);
	}
}

/** Leave every top-level comment a person made on a document held, as `leaveUnsent` does for one. */
export async function leaveUserCommentsUnsent(request: APIRequestContext, artifactId: string): Promise<void> {
	const listed = await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } });
	expect(listed.ok(), await listed.text()).toBeTruthy();
	const comments = (await listed.json()).comments as Array<{ id: string; author_kind: string; parent_id: string | null }>;
	for (const comment of comments) if (comment.author_kind === 'user' && !comment.parent_id) await leaveUnsent(request, comment.id);
}

/** The batches a document holds, by lane and state: what the server believes, not what a surface shows. */
export function batchRows(artifactId: string): Array<{ policy: string; state: string; items: number }> {
	const output = isolatedDb([
		'rows = connection.execute("SELECT delivery_policy, state, item_count FROM doc_feedback_batches WHERE author_kind=\'user\' AND artifact_id=? ORDER BY opened_at", (sys.argv[2],)).fetchall()',
		'print(json.dumps([{"policy": r[0], "state": r[1], "items": r[2]} for r in rows]))',
	], artifactId);
	return JSON.parse(output);
}

/** Comments a document still holds in one lane (open batches only). */
export function heldIn(artifactId: string, policy: 'document-held' | 'image-held'): number {
	return batchRows(artifactId).filter((row) => row.policy === policy && row.state === 'open').reduce((sum, row) => sum + row.items, 0);
}

/** Batches the server delivered for a document, by lane. */
export function deliveredBatches(artifactId: string, policy: 'document-held' | 'image-held'): number {
	return batchRows(artifactId).filter((row) => row.policy === policy && row.state === 'delivered').length;
}

/** Make one isolated mock batch due now; the next real feedback write wakes the continuation sweep. */
export function makeHeldBatchDue(artifactId: string, policy: 'document-held' | 'image-held'): void {
	isolatedDb([
		"changed = connection.execute(\"UPDATE doc_feedback_batches SET closes_at='2000-01-01T00:00:00+00:00' WHERE artifact_id=? AND author_kind='user' AND delivery_policy=? AND state='open'\", (sys.argv[2], sys.argv[3])).rowcount",
		'assert changed == 1, changed',
	], artifactId, policy);
}

// ---- The three senders -------------------------------------------------------------------------------------------

const SEND_NAME = /^Send( all)? \d+ comments?( to .+)?$/;

/** The open document's own header Send (its document-held lane). */
export const headerSend = (page: Page): Locator => page.locator('.document-header').getByRole('button', { name: SEND_NAME });
/** The image viewer bar's Send (its image-held lane), for an image over a document or an image file. */
export const imageBarSend = (page: Page): Locator => page.locator('.image-comments .sending-now').getByRole('button', { name: SEND_NAME });
/** The chat's Send for the thread's document-held comments whose header is not on screen. */
export const chatSendGroup = (page: Page): Locator => page.getByRole('group', { name: 'Document comments waiting to be sent' });
export const chatSend = (page: Page): Locator => chatSendGroup(page).getByRole('button', { name: SEND_NAME });

export type Sender = { where: 'header' | 'image-bar' | 'chat'; label: string };

/**
 * Every Send a person could see and press right now, wherever it is: a button whose text matches Send N comments with
 * a box larger than 1 px under no hidden, clipped or collapsed ancestor. Playwright's `toBeVisible` ignores `clip`, so a
 * clipped Send once passed as visible.
 */
export function visibleSenders(page: Page): Promise<Sender[]> {
	return page.evaluate(() => {
		const hidden = (element: Element) => {
			for (let node: Element | null = element; node; node = node.parentElement) {
				const style = getComputedStyle(node);
				if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return true;
				if (style.position === 'absolute' && style.clip !== 'auto' && style.clip !== '') return true;
				// A `display: contents` wrapper has no box of its own; its children are laid out in its parent.
				if (style.display === 'contents') continue;
				const box = node.getBoundingClientRect();
				if (style.overflow !== 'visible' && (box.width <= 1 || box.height <= 1)) return true;
			}
			return false;
		};
		const found: Array<{ where: 'header' | 'image-bar' | 'chat'; label: string }> = [];
		for (const button of document.querySelectorAll<HTMLElement>('button')) {
			const text = button.textContent?.replace(/\s+/g, ' ').trim() ?? '';
			if (!/^Send( all)? \d+ comments?$/.test(text)) continue;
			const box = button.getBoundingClientRect();
			if (box.width <= 1 || box.height <= 1 || hidden(button)) continue;
			// A button off the screen (a hidden pane parked aside) is not one a person sees.
			if (box.right <= 0 || box.bottom <= 0 || box.left >= innerWidth || box.top >= innerHeight) continue;
			// Nor is one covered by another surface (an image viewer laid over the document's header).
			const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
			if (!top || !(button.contains(top) || top.contains(button))) continue;
			const where = button.closest('.document-header') ? 'header'
				: button.closest('.image-comments') ? 'image-bar'
				: button.closest('[aria-label="Document comments waiting to be sent"]') ? 'chat'
				: null;
			if (where) found.push({ where, label: text });
		}
		return found;
	});
}

/**
 * Records, from now until `stop()`, every distinct label the page's Send buttons show (and every distinct set of
 * senders), so a flash of a wrong count between two settled states is caught even when it lasts one frame.
 */
export async function watchSenders(page: Page): Promise<{ stop: () => Promise<string[]> }> {
	await page.evaluate(() => {
		const seen = new Set<string>();
		const read = () => {
			for (const button of document.querySelectorAll<HTMLElement>('button')) {
				const text = button.textContent?.replace(/\s+/g, ' ').trim() ?? '';
				if (!/^Send( all)? \d+ comments?$/.test(text)) continue;
				const where = button.closest('.document-header') ? 'header'
					: button.closest('.image-comments') ? 'image-bar'
					: button.closest('[aria-label="Document comments waiting to be sent"]') ? 'chat'
					: 'other';
				seen.add(`${where}: ${text}`);
			}
		};
		const observer = new MutationObserver(read);
		observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true });
		read();
		(window as unknown as { __senderWatch: { seen: Set<string>; observer: MutationObserver } }).__senderWatch = { seen, observer };
	});
	return {
		stop: () => page.evaluate(() => {
			const watch = (window as unknown as { __senderWatch: { seen: Set<string>; observer: MutationObserver } }).__senderWatch;
			watch.observer.disconnect();
			return [...watch.seen].sort();
		}),
	};
}

/** The POSTs to the send endpoints a page makes, so a spec can prove a close went out once. */
export function watchSendRequests(page: Page): { document: string[]; thread: string[] } {
	const seen = { document: [] as string[], thread: [] as string[] };
	page.on('request', (outgoing) => {
		if (outgoing.method() !== 'POST') return;
		const pathname = new URL(outgoing.url()).pathname;
		if (pathname === '/api/doc-feedback/send') seen.document.push(outgoing.postData() ?? '');
		else if (pathname === '/api/doc-feedback/thread/send') seen.thread.push(outgoing.postData() ?? '');
	});
	return seen;
}

/**
 * Sends with the keyboard: focuses `send`, presses Enter and waits for the send request and for the control to go.
 * `whileSent` runs the moment it has gone (the "sent" line holds for 1.5 s only). `focusAfter` proves where
 * keyboard focus lands after the disappearing Send. Returns the send request.
 */
export async function sendByKeyboard(page: Page, send: Locator, whileSent?: () => Promise<void>, focusAfter?: Locator) {
	await send.focus();
	const sent = page.waitForRequest((req) => req.method() === 'POST' && req.url().includes('/api/doc-feedback/send'));
	await page.keyboard.press('Enter');
	const request = await sent;
	await expect(send).toHaveCount(0);
	await whileSent?.();
	if (focusAfter) await expect(focusAfter).toBeFocused();
	return request;
}

/** The toast a send raised, by its exact wording. */
export const sentToast = (page: Page, text: string): Locator => page.locator('.toast').filter({ hasText: text });

// ---- Fixtures ----------------------------------------------------------------------------------------------------

export interface TextDocument { artifactId: string; path: string; fileName: string }

/** A commentable document owned by `threadId`; a caller may supply a longer body for composed-canvas evidence. */
export async function seedTextDocument(request: APIRequestContext, threadId: string, title: string, suffix: string, text = 'A commentable document.\n'): Promise<TextDocument> {
	const rootsResponse = await request.get(`/api/files/tree?token=${tok()}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}-${suffix}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${tok()}`, {
		data: { path: docPath, text, thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { artifactId: (await seeded.json()).artifact.id as string, path: docPath, fileName };
}

/** Opens `documents` as the thread's canvas tabs (the one at `active` shown) before the app loads; `visible: false` starts it collapsed. */
export async function installWorkspace(page: Page, threadId: string, documents: TextDocument[], active = 0, { visible = true, splitWidth = 420 } = {}): Promise<void> {
	await page.addInitScript(({ threadId, tabs, active, visible, splitWidth }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: tabs.map((tab, index) => ({
					instanceId: `doc-${index}`, artifactId: tab.artifactId, path: tab.path, title: tab.fileName,
					loadedRevision: null, latestRevision: null, status: 'loading',
				})),
				activeTabId: `doc-${active}`, visible, activeSurface: 'artifact',
			} },
			splitWidth,
		}));
	}, { threadId, tabs: documents, active, visible, splitWidth });
}

/** Signs in and opens the thread called `title`, and waits for the chat's read of what the thread holds. */
export async function openThread(page: Page, threadId: string, title: string): Promise<void> {
	const read = page.waitForResponse((response) => response.request().method() === 'GET' && new URL(response.url()).pathname === `/api/doc-feedback/thread/${threadId}`);
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await read;
}

/** The Sends on screen, as `where: label` lines in a stable order. */
export async function senderLines(page: Page): Promise<string[]> {
	return (await visibleSenders(page)).map((sender) => `${sender.where}: ${sender.label}`).sort();
}

/**
 * The Sends on screen settle to exactly `expected`: a Send slides in and out of its row, so the first read can see one
 * mid-slide. It is read again after a beat, so a Send that is only passing through is not taken for the settled state.
 */
export async function expectSenders(page: Page, expected: string[], message = 'the Sends on screen'): Promise<void> {
	await expect.poll(() => senderLines(page), { message }).toEqual(expected);
	await page.waitForTimeout(300);
	expect(await senderLines(page), message).toEqual(expected);
}
