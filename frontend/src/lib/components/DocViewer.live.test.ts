import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate, textDigest } from '$lib/stores/documentWire';
import { LIVE_OPEN_WAIT_MS, liveDocumentBindings, startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, existingDraft, flushDraft, seedDraft, setDraftText } from '$lib/stores/docSaveQueue';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
// Product Experience approved this disclosure exactly as written.
const SNAPSHOT_REFUSAL_MESSAGE = "This tab shows the last saved version of the file. Changes from this browser that weren't saved yet aren't shown here.";
const target = { instanceId: 'live-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
const docs: Y.Doc[] = [];

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(target);
	wire.frames.length = 0;
	for (const doc of docs.splice(0)) doc.destroy();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});
function render(onSignal?: (signal: unknown) => void) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target, onSignal } });
	flushSync();
}
const editorView = () => EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
async function serverOpen(text = 'server text') {
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
	const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	const server = new Y.Doc(); docs.push(server); server.getText('text').insert(0, text);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(text));
	return server;
}

describe('composed live viewer', () => {
	it('waits for first live sync and autosaves through the lane without HTTP whole-text writes', async () => {
		render();
		await serverOpen();
		expect(api.fetchContent).toHaveBeenCalledWith(target.path, expect.objectContaining({ liveOpen: true }));
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		view.dispatch({ changes: { from: 0, insert: 'local ' }, userEvent: 'input.type' });
		flushSync();
		expect(get(liveDocumentBindings)[target.instanceId].text.toString()).toBe('local server text');
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_flush')).toBe(true));
		expect(api.writeContent).not.toHaveBeenCalled();
		const request = wire.frames.findLast(frame => frame.type === 'doc_flush')!;
		// The lane's durable closure acks every batch it received before the flush returns (4.1).
		emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store',
			message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
		emit('doc_saved', { type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id,
			server_saved: true, pending: false, held: null, digest: await textDigest('local server text') });
		await vi.waitFor(() => expect(existingDraft(target)?.savedText).toBe('local server text'));
		expect(wire.frames.filter(frame => frame.type === 'doc_flush')).toHaveLength(1);
	});

	it('keeps a legacy dirty draft visible and opens live only after its validated save succeeds', async () => {
		seedDraft(target, 'old disk'); setDraftText(target, 'legacy local');
		let saved!: (value: unknown) => void;
		api.writeContent.mockReturnValue(new Promise(resolve => saved = resolve));
		render();
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledOnce());
		expect(host.querySelector('.cm-content')?.textContent).toBe('legacy local');
		expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
		saved({ saved: true, revision: 'new-disk' });
		await serverOpen('legacy local');
		expect(api.writeContent).toHaveBeenCalledWith(target.path, 'legacy local', expect.any(Object));
	});

	it('preserves a dirty legacy draft after another viewer makes the document live', async () => {
		seedDraft(target, 'old disk'); setDraftText(target, 'legacy local');
		api.writeContent.mockRejectedValue(new Error('409 {"detail":{"code":"document_live","message":"This document is live"}}'));
		render();
		await vi.waitFor(() => expect(existingDraft(target)?.status).toBe('error'));
		expect(existingDraft(target)?.editText).toBe('legacy local');
		expect(host.querySelector('.cm-content')?.textContent).toBe('legacy local');
		expect(wire.frames.filter(frame => ['doc_open', 'doc_update', 'doc_replace'].includes(frame.type))).toEqual([]);
		expect(host.textContent).toContain('This document is live');
	});

	it.each(['store_damaged', 'store_unavailable'])('keeps tab text and shows a %s refusal verbatim with the allowed actions', async code => {
		seedDraft(target, 'kept tab text');
		const title = 'Live editing is unavailable for this document.';
		const message = 'Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.';
		api.fetchContent.mockRejectedValue(Object.assign(new Error(message), { code, title }));
		const copy = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal('navigator', { clipboard: { writeText: copy } });
		render();
		await vi.waitFor(() => expect(host.textContent).toContain(message));
		expect(host.textContent).toContain(title);
		expect(existingDraft(target)?.editText).toBe('kept tab text');
		expect(host.querySelector('.cm-content')?.textContent).toBe('kept tab text');
		const buttons = [...host.querySelectorAll('button')];
		buttons.find(button => button.textContent?.trim() === 'Copy')!.click();
		await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('kept tab text'));
		const retry = buttons.find(button => button.textContent?.trim() === 'Retry');
		expect(Boolean(retry)).toBe(code === 'store_unavailable');
		expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
		if (retry) {
			api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
			retry.click();
			await serverOpen('kept tab text');
			expect(api.fetchContent).toHaveBeenCalledTimes(2);
			expect(api.fetchContent).toHaveBeenLastCalledWith(target.path, expect.objectContaining({ liveOpen: true }));
		}
	});

	it('reopens a not_connected refusal only after the connection returns and clears the refusal on success', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
		connectionState.set('connecting');
		render();
		await Promise.resolve(); await Promise.resolve();
		vi.advanceTimersByTime(LIVE_OPEN_WAIT_MS);
		await Promise.resolve(); await Promise.resolve();
		await vi.waitFor(() => expect(host.textContent).toContain('Live editing is not connected.'));
		expect(host.textContent).toContain('It reopens when the connection returns.');
		expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
		vi.useRealTimers();
		connectionState.set('connected');
		await serverOpen();
		expect(host.textContent).not.toContain('Live editing is not connected.');
		expect(editorView().state.readOnly).toBe(false);
	});

	it.each(['store_damaged', 'store_unavailable', 'setup_failed'] as const)('does not reopen a %s refusal when the connection returns', async code => {
		const title = 'Live editing is unavailable for this document.';
		if (code !== 'setup_failed') {
			api.fetchContent.mockRejectedValue(Object.assign(new Error('Live editing could not start.'), { code, title }));
		}
		render();
		if (code === 'setup_failed') {
			await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
			const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
			emit('doc_error', { type: 'doc_error', request_id: request.request_id, code, title, message: 'The shared target could not be prepared.' });
		}
		await vi.waitFor(() => expect(host.textContent).toContain(title));
		const opens = wire.frames.filter(frame => frame.type === 'doc_open').length;
		const loads = api.fetchContent.mock.calls.length;
		// Flush between the two so the viewer sees a real transition into connected.
		connectionState.set('connecting'); flushSync();
		connectionState.set('connected'); flushSync();
		await Promise.resolve(); await Promise.resolve();
		expect(wire.frames.filter(frame => frame.type === 'doc_open')).toHaveLength(opens);
		expect(api.fetchContent).toHaveBeenCalledTimes(loads);
	});

	it.each(['not_connected', 'store_unavailable'])('keeps an unsaved edit through a %s refusal and its Retry, never replacing it with server text', async code => {
		seedDraft(target, 'disk copy'); setDraftText(target, 'my unsaved edit');
		api.writeContent.mockResolvedValue({ saved: true, revision: 'r-edit' });
		// The HTTP snapshot predates the edit; it must never reach the editor.
		api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
		const text = () => host.querySelector('.cm-content')?.textContent;
		if (code === 'not_connected') {
			vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
			connectionState.set('connecting');
		} else {
			api.fetchContent.mockRejectedValueOnce(Object.assign(new Error('Live editing could not start. Text in this tab is kept.'),
				{ code, title: 'Live editing is unavailable for this document.' }));
		}
		render();
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledWith(target.path, 'my unsaved edit', expect.any(Object)));
		if (code === 'not_connected') vi.advanceTimersByTime(LIVE_OPEN_WAIT_MS);
		await vi.waitFor(() => expect(host.textContent).toContain('Text in this tab is kept'));
		vi.useRealTimers();
		expect(text()).toBe('my unsaved edit');
		expect(EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!.state.readOnly).toBe(true);
		expect(existingDraft(target)?.editText).toBe('my unsaved edit');
		expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
		connectionState.set('connected');
		if (code === 'store_unavailable') {
			[...host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Retry')!.click();
		}
		await serverOpen('my unsaved edit');
		expect(text()).toBe('my unsaved edit');
		expect(existingDraft(target)?.editText).toBe('my unsaved edit');
		expect(api.writeContent).toHaveBeenCalledOnce();
	});

	it.each([
		['store_damaged', true], ['store_unavailable', true], ['store_damaged', false], ['store_unavailable', false],
	] as const)('a %s refusal keeps the server body only while the tab shows its draft (draft: %s)', async (code, withDraft) => {
		if (withDraft) seedDraft(target, 'tab draft');
		const title = 'Live editing is unavailable for this document.';
		const message = 'Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.';
		const copy = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal('navigator', { clipboard: { writeText: copy } });
		render();
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
		emit('doc_error', { type: 'doc_error', request_id: request.request_id, code, title, message });
		await vi.waitFor(() => expect(host.textContent).toContain(title));
		const body = host.querySelector('.live-block[role="alert"] p')?.textContent;
		const shown = withDraft ? 'tab draft' : 'disk copy';
		expect(host.querySelector('.cm-content')?.textContent).toBe(shown);
		if (withDraft) expect(body).toBe(message);
		else {
			// The editor shows the file snapshot, never device text, so nothing here may claim the edit was kept.
			expect(body).toBe(SNAPSHOT_REFUSAL_MESSAGE);
			expect(host.textContent).not.toContain('kept');
		}
		const buttons = [...host.querySelectorAll('button')];
		buttons.find(button => button.textContent?.trim() === 'Copy')!.click();
		await vi.waitFor(() => expect(copy).toHaveBeenCalledWith(shown));
		expect(buttons.some(button => button.textContent?.trim() === 'Retry')).toBe(code === 'store_unavailable');
	});

	// Product Experience approved the new-open non-live row exactly as written; it has no actions.
	it.each(['http', 'ws'] as const)('opens a damaged store (%s answer) non-live and editable with one header row, saving through the legacy route', async transport => {
		const title = 'Live editing is unavailable for this document.';
		const message = "Its saved editing history could not be read. You can edit and save this file without live editing. Earlier changes stored only in that history or a browser copy aren't shown here.";
		const damaged = { live: false, live_unavailable: 'store_damaged' };
		if (transport === 'http') api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null, ...damaged });
		api.writeContent.mockResolvedValue({ saved: true, revision: 'r-typed' });
		render();
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
		emit('doc_opened', { type: 'doc_opened', request_id: request.request_id, ...(transport === 'ws' ? damaged : { live: false }) });
		await vi.waitFor(() => expect(host.textContent).toContain(message));
		const rows = [...host.querySelectorAll('.live-block')].filter(row => row.textContent?.includes(title));
		expect(rows).toHaveLength(1);
		expect(rows[0].querySelector('p')?.textContent).toBe(message);
		expect(rows[0].querySelectorAll('button')).toHaveLength(0);
		expect([...host.querySelectorAll('button')].some(button => button.textContent?.trim() === 'Retry')).toBe(false);
		expect(get(liveDocumentBindings)).toEqual({});
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		expect(view.state.readOnly).toBe(false);
		view.dispatch({ changes: { from: 0, insert: 'typed ' }, userEvent: 'input.type' });
		flushSync();
		await flushDraft(target);
		expect(api.writeContent).toHaveBeenCalledWith(target.path, 'typed disk copy', expect.any(Object));
		expect(wire.frames.filter(frame => ['doc_update', 'doc_flush'].includes(frame.type))).toEqual([]);
	});

	it('clears the damaged-store header when the next load is refused', async () => {
		const title = 'Live editing is unavailable for this document.';
		api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null, live: false, live_unavailable: 'store_damaged' });
		render();
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		emit('doc_opened', { type: 'doc_opened', request_id: wire.frames.findLast(frame => frame.type === 'doc_open')!.request_id, live: false });
		await vi.waitFor(() => expect(host.textContent).toContain('You can edit and save this file without live editing.'));
		const unavailable = "Its saved editing history can't be opened right now. Text in this tab is kept. Retry, or copy it to keep working.";
		api.fetchContent.mockRejectedValue(Object.assign(new Error(unavailable), { code: 'store_unavailable', title }));
		(component as unknown as { reload: () => void }).reload();
		await vi.waitFor(() => expect(host.textContent).toContain("can't be opened right now"));
		expect(host.textContent).not.toContain('You can edit and save this file without live editing.');
	});

	it('retries a typed setup_failed WS refusal through HTTP admission before opening again', async () => {
		render();
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
		emit('doc_error', { type: 'doc_error', request_id: request.request_id, code: 'setup_failed',
			title: 'Setup failed', message: 'The shared target could not be prepared.' });
		// No draft: the tab shows the file snapshot, so the body is the snapshot disclosure under the server's title.
		await vi.waitFor(() => expect(host.textContent).toContain('Setup failed'));
		expect(host.textContent).toContain(SNAPSHOT_REFUSAL_MESSAGE);
		[...host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Retry')!.click();
		await vi.waitFor(() => expect(wire.frames.filter(frame => frame.type === 'doc_open')).toHaveLength(2));
		await serverOpen();
		expect(api.fetchContent).toHaveBeenCalledTimes(2);
		for (const [, options] of api.fetchContent.mock.calls) expect(options.liveOpen).toBe(true);
	});

	it('paints a returning tab from its saved text at once, read-only with no read-only mark until the live document binds', async () => {
		seedDraft(target, 'saved text');
		const onSignal = vi.fn();
		render(onSignal);
		expect(host.querySelector('.md-skel')).toBeNull();
		expect(host.querySelector('.cm-content')?.textContent).toBe('saved text');
		expect(editorView().state.readOnly).toBe(true);
		expect(host.querySelector('.readonly-note')).toBeNull();
		// The tab is told its text is shown, but nothing about write access, so it shows no read-only mark meanwhile.
		expect(onSignal).toHaveBeenCalledWith(expect.objectContaining({ status: 'ready', shown: true }));
		expect(onSignal.mock.calls[0][0]).not.toHaveProperty('writable');
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		expect(editorView().state.readOnly).toBe(true);
		// The live text matches the saved text, so the editor shows no change; only write access arrives.
		await serverOpen('saved text');
		await vi.waitFor(() => expect(editorView().state.readOnly).toBe(false));
		expect(get(liveDocumentBindings)[target.instanceId].text.toString()).toBe('saved text');
		expect(host.querySelector('.readonly-note')).toBeNull();
	});

	it('replaces a returning tab\'s saved text with the live text when the document changed meanwhile', async () => {
		seedDraft(target, 'saved text');
		render();
		expect(host.querySelector('.cm-content')?.textContent).toBe('saved text');
		await serverOpen('edited elsewhere');
		expect(editorView().state.readOnly).toBe(false);
		expect(existingDraft(target)?.editText).toBe('edited elsewhere');
	});

	it('shows the loading state on a first open, which has no saved text to paint', () => {
		api.fetchContent.mockReturnValue(new Promise(() => {}));
		const onSignal = vi.fn();
		render(onSignal);
		expect(host.querySelector('.md-skel')).not.toBeNull();
		expect(host.querySelector('.cm-editor')).toBeNull();
		expect(onSignal).toHaveBeenCalledWith(expect.objectContaining({ status: 'loading' }));
	});
});
