import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from '$lib/stores/documentWire';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, flushDraft } from '$lib/stores/docSaveQueue';

// 5.7(a) rev 16 countercase 1: an existing live session never takes the non-live path; its reconnect refuses it in place.
const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
const device = vi.hoisted(() => ({ destroy: vi.fn(async () => {}) }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = device.destroy;
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
const target = { instanceId: 'reconnect-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
const TITLE = 'Live editing is unavailable for this document.';
// Product Experience approved these existing-session bodies (store_damaged 08:22 PDT, reasonless 12:28 PDT).
const DAMAGED = 'Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.';
const NOT_LIVE = 'Text in this tab is kept. Copy it to keep working.';
const UNAVAILABLE = "Its saved editing history can't be opened right now. Text in this tab is kept. Retry, or copy it to keep working.";
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
// One server document per test, so every live answer carries the same history.
let server: Y.Doc;

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	server = new Y.Doc(); server.getText('text').insert(0, 'server text');
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(target);
	wire.frames.length = 0;
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
	device.destroy.mockClear();
	server.destroy();
});
const opens = () => wire.frames.filter(frame => frame.type === 'doc_open');
function answerLive(request: Record<string, any>) {
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store', request_id: request.request_id,
		update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
}
/** Opens live, types while disconnected, reconnects, and answers the reopen with `frame`. */
async function reconnectAnsweredWith(frame: Record<string, unknown>) {
	const copy = vi.fn().mockResolvedValue(undefined);
	vi.stubGlobal('navigator', { clipboard: { writeText: copy } });
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
	flushSync();
	await vi.waitFor(() => expect(opens()).toHaveLength(1));
	answerLive(opens()[0]);
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe('server text'));
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	connectionState.set('disconnected');
	view.dispatch({ changes: { from: 0, insert: 'offline ' }, userEvent: 'input.type' });
	flushSync();
	connectionState.set('connected');
	await vi.waitFor(() => expect(opens()).toHaveLength(2));
	const reopen = wire.frames.findLastIndex(frame => frame.type === 'doc_open');
	emit(String(frame.type), { ...frame, request_id: wire.frames[reopen].request_id });
	await vi.waitFor(() => expect(host.textContent).toContain(TITLE));
	return { copy, view, reopen };
}
const rows = () => [...host.querySelectorAll('.live-block[role="alert"]')];
const buttons = (row: Element) => [...row.querySelectorAll('button')].map(button => button.textContent?.trim());

describe('a live session whose reconnect is refused', () => {
	it.each([
		['store_damaged', { live_unavailable: 'store_damaged' }, DAMAGED],
		['reasonless server frame', {}, NOT_LIVE],
	] as const)(
		'a reconnect answered non-live (%s) keeps the session text read-only with Copy and no Retry', async (_reason, extra, message) => {
			const { copy, view, reopen } = await reconnectAnsweredWith({ type: 'doc_opened', live: false, ...extra });
			expect(rows()).toHaveLength(1);
			expect(rows()[0].querySelector('strong')?.textContent).toBe(TITLE);
			expect(rows()[0].querySelector('p')?.textContent).toBe(message);
			expect(host.textContent).not.toContain('Reconnecting');
			expect(buttons(rows()[0])).toEqual(['Copy']);
			expect(host.querySelector('.cm-content')?.textContent).toBe('offline server text');
			expect(view.state.readOnly).toBe(true);
			rows()[0].querySelector('button')!.click();
			await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('offline server text'));
			await flushDraft(target);
			expect(api.writeContent).not.toHaveBeenCalled();
			expect(api.fetchContent).toHaveBeenCalledOnce();
			expect(wire.frames.slice(reopen + 1).map(frame => frame.type)).toEqual([]);
			expect(device.destroy).not.toHaveBeenCalled();
		});

	it('refuses a mismatched live reconnect answer in place before rebinding the viewer', async () => {
		const { view, reopen } = await reconnectAnsweredWith({ type: 'doc_opened', live: true, writable: true,
			artifact_id: 'other-artifact', store_id: 'other-store', update: encodeUpdate(Y.encodeStateAsUpdate(server)),
			vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
		expect(wire.frames[reopen].expected_artifact_id).toBe('artifact');
		expect(rows()).toHaveLength(1);
		expect(rows()[0].querySelector('p')?.textContent).toBe(NOT_LIVE);
		expect(buttons(rows()[0])).toEqual(['Copy']);
		expect(host.querySelector('.cm-content')?.textContent).toBe('offline server text');
		expect(view.state.readOnly).toBe(true);
		expect(wire.frames.slice(reopen + 1)).toEqual([]);
		expect(device.destroy).not.toHaveBeenCalled();
	});

	it('a reconnect refused by a typed doc_error keeps the text read-only, offers Copy and Retry, and sends nothing', async () => {
		const { view, reopen } = await reconnectAnsweredWith({ type: 'doc_error', code: 'store_unavailable', title: TITLE, message: UNAVAILABLE });
		expect(rows()).toHaveLength(1);
		expect(rows()[0].querySelector('p')?.textContent).toBe(UNAVAILABLE);
		expect(buttons(rows()[0])).toEqual(['Copy', 'Retry']);
		expect(host.textContent).not.toContain('Disconnected');
		expect(host.querySelector('.cm-content')?.textContent).toBe('offline server text');
		expect(view.state.readOnly).toBe(true);
		await flushDraft(target);
		expect(wire.frames.slice(reopen + 1)).toEqual([]);
		expect(api.fetchContent).toHaveBeenCalledOnce();
		expect(api.writeContent).not.toHaveBeenCalled();
		expect(device.destroy).not.toHaveBeenCalled();
	});

	it('renders only curated setup copy on typed reconnect refusal and keeps full text Copy and Retry', async () => {
		const { copy, view, reopen } = await reconnectAnsweredWith({ type: 'doc_error', kind: 'setup_failed', code: 'setup_failed',
			title: TITLE, message: 'Document setup failed; open it again to retry (private fixture reason)' });
		expect(rows()).toHaveLength(1);
		expect(rows()[0].querySelector('p')?.textContent).toBe("Live editing couldn't start for this document.");
		expect(host.textContent).not.toContain('private fixture');
		expect(buttons(rows()[0])).toEqual(['Copy', 'Retry']);
		expect(view.state.readOnly).toBe(true);
		rows()[0].querySelector('button')!.click();
		await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('offline server text'));
		expect(wire.frames.slice(reopen + 1)).toEqual([]);
		expect(device.destroy).not.toHaveBeenCalled();
	});

	it("Retry re-runs the session's own open once, and a live answer clears the refusal and restores writing", async () => {
		const { view, reopen } = await reconnectAnsweredWith({ type: 'doc_error', code: 'store_unavailable', title: TITLE, message: UNAVAILABLE });
		[...rows()[0].querySelectorAll('button')].find(button => button.textContent?.trim() === 'Retry')!.click();
		await vi.waitFor(() => expect(opens()).toHaveLength(3));
		expect(wire.frames.slice(reopen + 1).map(frame => frame.type)).toEqual(['doc_open']);
		expect(api.fetchContent).toHaveBeenCalledOnce();
		// Reopening ends the refusal row (so Retry cannot ask twice) but writing waits for the live answer.
		await vi.waitFor(() => expect(host.textContent).toContain('Reconnecting'));
		expect(rows()).toHaveLength(0);
		expect(view.state.readOnly).toBe(true);
		answerLive(opens()[2]);
		await vi.waitFor(() => expect(view.state.readOnly).toBe(false));
		expect(rows()).toHaveLength(0);
		expect(host.querySelector('.cm-content')?.textContent).toBe('offline server text');
	});

	it('a Retry answered by a damaged store stays refused, with no Retry', async () => {
		await reconnectAnsweredWith({ type: 'doc_error', code: 'store_unavailable', title: TITLE, message: UNAVAILABLE });
		[...rows()[0].querySelectorAll('button')].find(button => button.textContent?.trim() === 'Retry')!.click();
		await vi.waitFor(() => expect(opens()).toHaveLength(3));
		emit('doc_opened', { type: 'doc_opened', request_id: opens()[2].request_id, live: false, live_unavailable: 'store_damaged' });
		await vi.waitFor(() => expect(rows()[0]?.querySelector('p')?.textContent).toBe(DAMAGED));
		expect(rows()).toHaveLength(1);
		expect(buttons(rows()[0])).toEqual(['Copy']);
		expect(EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!.state.readOnly).toBe(true);
		expect(api.fetchContent).toHaveBeenCalledOnce();
	});
});
