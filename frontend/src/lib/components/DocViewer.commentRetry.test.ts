// A new comment's draft posts under one id, minted on its first press of Comment and kept with the kept draft until the
// comment shows or Cancel is pressed. A POST the server stored but never answered must not store a second comment when
// Comment is pressed again; a draft edited since that press shows the stored version and keeps the changes.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import DocViewer from './DocViewer.svelte';
import { COMMENT_POST_TIMEOUT_MS, PostTimeoutError } from '$lib/api/boundedRequest';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from '$lib/stores/documentWire';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft } from '$lib/stores/docSaveQueue';
import { discardCommentDraft } from '$lib/stores/commentDraft';
import { keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { toasts } from '$lib/stores/toast';
import { docComments, EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import { posting, resetCommentPostTiming } from '$lib/stores/commentPostTiming';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { user } from '$lib/stores/auth';
const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn(), postComment: vi.fn() }));
vi.mock('$lib/api/docs', async original => ({ ...await original<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

/**
 * The document's text. Its `beta` (6-10) cannot be told apart: the other copy has the same text after it, so a comment on
 * it keeps no place and does not go on its own; Comment is pressed again after a failure. One on a passage it can be told
 * by is sent again without a press (the last test).
 */
const TEXT = 'alpha beta gamma and the rest of this line. alpha beta gamma and the rest of this line.';
const target = { instanceId: 'comment-retry-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let host: HTMLElement, component: ReturnType<typeof mount> | undefined, stop: () => void, server: Y.Doc | undefined;

/**
 * The server's comments, keyed by client id as its dedupe keys them: a second POST under a stored id answers with the
 * stored row. `unanswered` makes the next POST store its comment and then never answer (the client's bound trips);
 * `lookupFails` makes the look for it fail too, so the press ends failed with nothing known.
 */
const store = { rows: new Map<string, Record<string, unknown>>(), unanswered: false, lookupFails: false };
const feedback = { open_count: 0, owner_label: null, undelivered_batches: [] };
beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	vi.spyOn(EditorView.prototype, 'posAtCoords').mockImplementation(function (this: EditorView) { return this.state.selection.main.head; });
	vi.spyOn(toasts, 'show').mockReturnValue('refusal');
	vi.spyOn(console, 'error').mockImplementation(() => {});
	connectionState.set('connected'); stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockImplementation(async (_artifact: string, within?: number) => {
		if (within && store.lookupFails) throw new PostTimeoutError(within);
		return { comments: [...store.rows.values()], feedback };
	});
	api.postComment.mockImplementation(async (_artifact: string, fields: Record<string, any>) => {
		const stored = store.rows.get(fields.client_id);
		if (stored) return stored;
		const row = { id: fields.client_id, artifact_id: 'artifact', body: fields.body.trim(), anchor_text: fields.anchor_text, from_off: fields.from_off,
			to_off: fields.to_off, author: 'you', author_kind: 'user', created_at: '2026-11-18T00:00:00Z', parent_id: null, review: false, resolved: false,
			replies: [], tagged_agent_ids: [...new Set<string>(fields.tagged_agent_ids ?? [])].sort() };
		store.rows.set(fields.client_id, row);
		if (store.unanswered) { store.unanswered = false; throw new PostTimeoutError(COMMENT_POST_TIMEOUT_MS); }
		return row;
	});
});
afterEach(async () => {
	if (component) await unmount(component); component = undefined;
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
	await Promise.resolve(); await Promise.resolve();
	host?.remove(); stop(); server?.destroy(); server = undefined; clearDraft(target); discardCommentDraft(); keptCommentDrafts.reset(); docComments.set({});
	wire.frames.length = 0; store.rows.clear(); store.unanswered = false; store.lookupFails = false; resetCommentPostTiming();
	vi.restoreAllMocks(); vi.unstubAllGlobals(); for (const fn of Object.values(api)) fn.mockReset();
});

async function mountViewer() {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } }); flushSync();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
}
/** A live document: the comment's range is captured (`doc_positions`) before its POST. */
async function openLive() {
	await mountViewer();
	const ask = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	server = new Y.Doc(); server.getText('text').insert(0, TEXT);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: ask.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(TEXT));
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
}
/** A document that is not live: the comment posts with the offsets on screen. */
async function openSaved(text = TEXT) {
	api.fetchContent.mockResolvedValue({ text, writable: true, artifact: { id: 'artifact' }, freshness: null, live: false });
	await mountViewer();
	emit('doc_opened', { type: 'doc_opened', request_id: wire.frames.findLast(frame => frame.type === 'doc_open')!.request_id, live: false });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(text));
}
const field = () => document.querySelector<HTMLTextAreaElement>('.composer textarea');
const submit = () => document.querySelector<HTMLButtonElement>('.composer .submit')!;
const alert = () => document.querySelector('.composer [role="alert"]')?.textContent ?? null;
/** Captures: the `doc_positions` asks that reserve a comment's range. */
const asks = () => wire.frames.filter(frame => frame.type === 'doc_positions' && frame.reserve);
const posted = () => api.postComment.mock.calls.map(([, fields]) => fields as Record<string, any>);
const shown = () => (get(docComments).artifact ?? []).map(comment => comment.text);
async function compose(body: string) {
	EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!.dispatch({ selection: { anchor: 6, head: 10 } }); flushSync();
	await vi.waitFor(() => expect(host.querySelector('.sel-comment')).not.toBeNull());
	host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	await vi.waitFor(() => expect(field()).not.toBeNull());
	type(body);
}
function type(body: string) {
	const input = field()!;
	input.value = body; input.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
}
/** Press Comment; on a live document answer each capture it asks for. */
async function press(live: boolean) {
	const before = asks().length;
	submit().click();
	if (!live) return;
	await vi.waitFor(() => expect(asks().length).toBeGreaterThan(before));
	emit('doc_positions', { type: 'doc_positions', artifact_id: 'artifact', store_id: 'store', request_id: asks().at(-1)!.request_id, seq: asks().at(-1)!.seq, reserved: true });
}
/** The press stored its comment but got no answer, and the look for it failed: the press ends failed, text kept. */
async function unansweredPress(live: boolean) {
	store.unanswered = true; store.lookupFails = true;
	await press(live);
	await vi.waitFor(() => expect(submit().disabled).toBe(false));
	await vi.waitFor(() => expect(alert()).not.toBeNull());
	expect(store.rows.size).toBe(1);
	store.lookupFails = false;
}

for (const live of [true, false]) {
	const open = live ? openLive : openSaved;
	const kind = live ? 'a live document' : 'a document that is not live';

	it(`${kind}: a press after a POST the server stored but never answered shows that comment and stores no second one`, async () => {
		await open(); await compose('Too vague');
		await unansweredPress(live);
		expect(field()?.value).toBe('Too vague');
		const first = posted()[0].client_id;
		await press(false);
		await vi.waitFor(() => expect(field()).toBeNull());
		expect(store.rows.size).toBe(1);
		expect(shown()).toEqual(['Too vague']);
		// The live press finds its comment before capturing again; the saved one posts again under the same id.
		expect(posted().map(fields => fields.client_id)).toEqual(live ? [first] : [first, first]);
		expect(asks()).toHaveLength(live ? 1 : 0);
		expect(keptCommentDrafts.get(`document:${JSON.stringify(['project', target.path])}`)).toBeUndefined();
	});

	it(`${kind}: leg (i), the text edited after an unanswered press shows the first version and keeps the edit for a new comment`, async () => {
		await open(); await compose('Too vague');
		await unansweredPress(live);
		type('Too vague: name the owner');
		await press(false);
		await vi.waitFor(() => expect(alert()).toBe(EARLIER_VERSION_POSTED));
		expect(shown()).toEqual(['Too vague']);
		expect(field()?.value).toBe('Too vague: name the owner');
		expect(store.rows.size).toBe(1);
		await press(live);
		await vi.waitFor(() => expect(field()).toBeNull());
		expect([...store.rows.values()].map(row => row.body)).toEqual(['Too vague', 'Too vague: name the owner']);
		expect(shown().sort()).toEqual(['Too vague', 'Too vague: name the owner']);
		const ids = new Set(posted().map(fields => fields.client_id));
		expect(ids.size).toBe(2);
	});

	it(`${kind}: leg (ii), the same text pressed again after an unanswered press is delivered once`, async () => {
		await open(); await compose('Too vague  ');
		await unansweredPress(live);
		// Trailing spaces are not a change: the server stores the text stripped.
		type('Too vague');
		await press(false);
		await vi.waitFor(() => expect(field()).toBeNull());
		expect(store.rows.size).toBe(1);
		expect(shown()).toEqual(['Too vague']);
	});

	it(`${kind}: the draft keeps its one id across a reload and a tab switch, and Cancel lets the next comment mint a new one`, async () => {
		await open(); await compose('Too vague');
		await unansweredPress(live);
		const first = posted()[0].client_id;
		store.rows.clear();
		if (live) {
			// A tab switch: the live session outlives the viewer.
			await unmount(component!); component = undefined; host.remove();
			host = document.createElement('div'); document.body.appendChild(host);
			component = mount(DocViewer, { target: host, props: { target } }); flushSync();
			await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(TEXT));
		} else {
			(component as unknown as { reload: () => void }).reload();
			await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(TEXT));
		}
		await compose('Too vague');
		await press(live);
		await vi.waitFor(() => expect(field()).toBeNull());
		expect(posted().at(-1)!.client_id).toBe(first);
		await compose('Another');
		document.querySelector<HTMLButtonElement>('.composer .cancel')!.click(); flushSync();
		await compose('Another');
		await press(live);
		await vi.waitFor(() => expect(field()).toBeNull());
		expect(posted().at(-1)!.client_id).not.toBe(first);
	});
}

it('a live document: a capture refused because the id is in use looks again and shows the comment an earlier press stored', async () => {
	await openLive(); await compose('Too vague');
	await unansweredPress(true);
	// The first look misses (the stored row is not listed yet), so the press captures again under the same id.
	const rows = new Map(store.rows); store.rows.clear();
	const before = asks().length;
	submit().click();
	await vi.waitFor(() => expect(asks().length).toBeGreaterThan(before));
	const ask = asks().at(-1)!;
	expect(ask.reserve.client_id).toBe(posted()[0].client_id);
	for (const [id, row] of rows) store.rows.set(id, row);
	emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: ask.request_id, kind: 'identity_in_use' });
	await vi.waitFor(() => expect(field()).toBeNull());
	expect(api.postComment).toHaveBeenCalledOnce();
	expect(shown()).toEqual(['Too vague']);
});

it('a document that is not live: a comment on a passage it can be told by reads "Sending…" after an unanswered press, and the next connection sends it once under its id', async () => {
	user.set({ id: 'person-1', name: 'person-1' } as never);
	try {
		await openSaved('alpha beta gamma'); await compose('Too vague');
		store.unanswered = true; store.lookupFails = true;
		await press(false);
		await vi.waitFor(() => expect(api.postComment).toHaveBeenCalledOnce());
		const first = posted()[0].client_id;
		await vi.waitFor(() => expect(posting(first)).toBe(false));
		flushSync();
		expect(submit().textContent).toBe('Sending…');
		expect(alert()).toBeNull();
		store.lookupFails = false;
		connectionState.set('disconnected'); connectionState.set('connected');
		await unsentComments.settled();
		await vi.waitFor(() => expect(field()).toBeNull());
		expect(store.rows.size).toBe(1);
		expect(posted().map(fields => fields.client_id)).toEqual([first, first]);
		expect(shown()).toEqual(['Too vague']);
	} finally {
		user.set(null);
	}
});
