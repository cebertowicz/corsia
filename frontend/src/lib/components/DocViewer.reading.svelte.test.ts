import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import { deleteCharBackward } from '@codemirror/commands';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import DocViewer from './DocViewer.svelte';
import DocumentBarTestHost from './DocumentBarTestHost.test.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate, textDigest } from '$lib/stores/documentWire';
import { liveDocumentBindings, startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { commentDraft, discardCommentDraft } from '$lib/stores/commentDraft';
import { selectionRange } from '$lib/stores/commentSelection';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async original => ({ ...await original<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
// The bar (Comments, Details, presence) is the phone header's form, which these tests read: above phone width the row is in the document.
vi.mock('$lib/stores/viewport', async original => {
	const { readable } = await import('svelte/store');
	return { ...await original<typeof import('$lib/stores/viewport')>(), isMobile: readable(true) };
});
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
const target = { instanceId: 'reading-viewer', path: '/documents/read.md', name: 'read.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
const DOC = 'intro\n\n| Item | Owner |\n|---|---|\n| Alpha | Dana |\n\nafter\n';
let host: HTMLElement, component: ReturnType<typeof mount> | undefined, bar: ReturnType<typeof mount> | undefined;
let stop: () => void, server: Y.Doc;
let primaryTouch = true;
beforeEach(() => {
	primaryTouch = true;
	vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query === '(pointer: coarse)' && primaryTouch, media: query, addEventListener() {}, removeEventListener() {} })));
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
	Range.prototype.getBoundingClientRect = () => new DOMRect();
	HTMLElement.prototype.scrollIntoView = vi.fn();
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue({ left: 20, right: 30, top: 20, bottom: 30 });
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: DOC, writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	if (component) await unmount(component);
	if (bar) await unmount(bar);
	component = undefined; bar = undefined;
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
	for (const frame of wire.frames.filter(frame => frame.type === 'doc_flush')) emit('doc_saved', {
		type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: frame.request_id, server_saved: true, pending: false, held: null });
	await Promise.resolve();
	host?.remove(); stop(); server?.destroy(); clearDraft(target); discardCommentDraft(); wire.frames.length = 0;
	document.getSelection()?.removeAllRanges();
	vi.restoreAllMocks(); vi.unstubAllGlobals();
	for (const fn of Object.values(api)) fn.mockReset();
});
async function open() {
	const props = $state({ target, offScreen: false, covered: false });
	host = document.body.appendChild(document.createElement('div'));
	component = mount(DocViewer, { target: host, props });
	bar = mount(DocumentBarTestHost, { target: host }); flushSync();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
	const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	server = new Y.Doc(); server.getText('text').insert(0, DOC);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('intro'));
	flushSync();
	return props;
}
const view = () => EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
function click(text: string) {
	const button = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === text)!;
	expect(button, `button ${text}`).toBeTruthy();
	button.click(); flushSync();
}
function enter() {
	host.querySelector<HTMLButtonElement>('[data-document-details]')!.click(); flushSync();
	click('Edit');
}
function assertReading(expected = DOC) {
	expect(view().state.facet(EditorView.editable)).toBe(false);
	expect(view().state.readOnly).toBe(true);
	expect(view().contentDOM.getAttribute('tabindex')).toBe('0');
	view().dispatch({ selection: { anchor: 0, head: 5 } });
	deleteCharBackward(view());
	expect(view().state.doc.toString()).toBe(expected);
}
function remoteAppend(text: string) {
	const vector = Y.encodeStateVector(server);
	server.getText('text').insert(server.getText('text').length, text);
	emit('doc_update', { type: 'doc_update', artifact_id: 'artifact', store_id: 'store', update: encodeUpdate(Y.encodeStateAsUpdate(server, vector)), vector: encodeUpdate(Y.encodeStateVector(server)) });
	flushSync();
}
describe('actual touch mode producer and document/cell consumers', () => {
	it.each(['loading', 'error', 'readonly'] as const)('keeps existing %s feedback and disables Edit', async state => {
		if (state === 'loading') api.fetchContent.mockImplementation(() => new Promise(() => {}));
		if (state === 'error') api.fetchContent.mockRejectedValue(new Error('Load failed'));
		if (state === 'readonly') api.fetchContent.mockResolvedValue({ text: DOC, writable: false, artifact: { id: 'artifact' }, freshness: null });
		host = document.body.appendChild(document.createElement('div'));
		component = mount(DocViewer, { target: host, props: { target } });
		bar = mount(DocumentBarTestHost, { target: host }); flushSync();
		if (state === 'error') await vi.waitFor(() => expect(host.querySelector('.load-error')).not.toBeNull());
		if (state === 'readonly') {
			await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
			const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
			emit('doc_opened', { type: 'doc_opened', request_id: request.request_id, artifact_id: 'artifact', live: false, writable: false });
			await vi.waitFor(() => expect(host.querySelector('.cm-content')).not.toBeNull());
		}
		host.querySelector<HTMLButtonElement>('[data-document-details]')!.click(); flushSync();
		expect(host.querySelector<HTMLButtonElement>('.p-edit')?.disabled).toBe(true);
		if (state === 'readonly') assertReading();
	});
	it.each([320, 393, 768])('opens primary touch in Reading at %spx and protects body and cells', async width => {
		vi.stubGlobal('innerWidth', width);
		await open(); assertReading();
		const cell = await vi.waitFor(() => {
			const td = host.querySelector('td'); if (!td) throw new Error('table not drawn'); return td;
		});
		cell.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
		const first = host.querySelector('.doc-panel button');
		expect(first?.textContent?.trim()).toBe('Edit');
	});
	it.each([768, 1440])('keeps primary mouse editing without mode controls at %spx', async width => {
		primaryTouch = false; vi.stubGlobal('innerWidth', width);
		await open();
		expect(view().state.readOnly).toBe(false);
		expect(view().state.facet(EditorView.editable)).toBe(true);
		expect(host.querySelector('.p-edit, .bar-done, .touch-comment-bar')).toBeNull();
	});
	it('keeps the opening mode across media/width changes and uses explicit Edit/Done', async () => {
		await open(); primaryTouch = false; window.dispatchEvent(new Event('resize')); flushSync(); assertReading();
		enter(); expect(view().state.readOnly).toBe(false);
		window.dispatchEvent(new Event('resize')); flushSync(); expect(view().state.readOnly).toBe(false);
		expect(host.querySelector('.bar-presence')?.hasAttribute('hidden')).toBe(true);
		click('Done'); assertReading();
		expect(host.querySelector('.bar-presence')?.hasAttribute('hidden')).toBe(false);
	});
	it('clears old body and cell ranges at both mode boundaries without focusing document text', async () => {
		await open();
		const cell = await vi.waitFor(() => { const td = host.querySelector('td'); if (!td) throw new Error('no cell'); return td; });
		const range = document.createRange(); range.selectNodeContents(cell);
		document.getSelection()!.addRange(range);
		enter();
		expect(document.getSelection()?.toString()).toBe('');
		expect(document.activeElement).not.toBe(view().contentDOM);
		view().dispatch({ selection: { anchor: 1, head: 4 } }); view().focus();
		click('Done');
		expect(view().state.selection.main.empty).toBe(true);
		expect(document.activeElement).not.toBe(view().contentDOM);
		assertReading();
	});
	it.each(['Done', 'offScreen', 'background'].flatMap(exit => ['clean', 'pending', 'failed'].flatMap(save => ['body', 'cell'].map(surface => ({ exit, save, surface })))))('keeps $surface writing and $save save through $exit, with arriving live text', async ({ exit, save, surface }) => {
		const props = await open(); enter();
		let staleCell: EditorView | undefined;
		if (surface === 'body') view().dispatch({ changes: { from: DOC.length, insert: 'local' }, userEvent: 'input.type' });
		else {
			const cell = await vi.waitFor(() => { const td = [...host.querySelectorAll('td')].find(el => el.textContent === 'Dana'); if (!td) throw new Error('no cell'); return td; });
			cell.click();
			const inner = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-md-cell-editor .cm-editor')!)!;
			inner.dispatch({ changes: { from: inner.state.doc.length, insert: 'local' }, userEvent: 'input.type' });
			staleCell = inner;
		}
		flushSync();
		const edited = view().state.doc.toString();
		expect(edited).toContain('local');
		expect(existingDraft(target)?.editText).toBe(edited);
		const saving = flushDraft(target);
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_flush')).toBe(true));
		const request = wire.frames.findLast(frame => frame.type === 'doc_flush')!;
		if (save === 'clean') {
			emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
			emit('doc_saved', { type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id, server_saved: true, pending: false, held: null, digest: await textDigest(edited) });
			expect(await saving).toBe(true);
		}
		if (save === 'failed') {
			emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id, kind: 'file_write_failed', message: 'Write refused for this test' });
			expect(await saving).toBe(false);
		}
		flushSync();
		const before = existingDraft(target)!;
		if (save === 'failed') expect(before.status).toBe('error');
		if (save === 'pending') expect(before.status).toBe('saving');
		if (exit === 'Done') click('Done');
		if (exit === 'offScreen') { props.offScreen = true; flushSync(); props.offScreen = false; flushSync(); }
		if (exit === 'background') {
			vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
			document.dispatchEvent(new Event('visibilitychange')); flushSync();
		}
		expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
		if (staleCell) {
			for (const key of ['z', 'y', 'Tab', 'Enter']) staleCell.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key, ctrlKey: key.length === 1, bubbles: true, cancelable: true }));
			staleCell.dispatch({ changes: { from: 0, insert: 'stale' }, userEvent: 'input.type' });
		}
		expect(view().state.doc.toString()).toBe(edited);
		expect(existingDraft(target)?.status).toBe(before.status);
		expect(existingDraft(target)?.error).toBe(before.error);
		if (save === 'failed') expect(host.textContent).toContain(before.error);
		remoteAppend(' remote');
		await vi.waitFor(() => expect(view().state.doc.toString()).toContain(' remote'));
		const current = view().state.doc.toString();
		expect(current).toContain('local'); assertReading(current);
		expect(existingDraft(target)?.editText).toBe(current);
		expect(get(liveDocumentBindings)[target.instanceId].text.toString()).toBe(current);
		if (save !== 'clean') {
			const recovered = save === 'pending' ? saving : flushDraft(target);
			const latest = wire.frames.findLast(frame => frame.type === 'doc_flush')!;
			emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
			emit('doc_saved', { type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: latest.request_id, server_saved: true, pending: false, held: null, digest: await textDigest(current) });
			expect(await recovered).toBe(true);
			expect(existingDraft(target)?.editText).toBe(current);
			expect(existingDraft(target)?.error).toBe('');
		}
	});
	it('keeps Edit through image cover/back, but background while covered still ends Edit', async () => {
		const props = await open(); enter();
		props.covered = true; props.offScreen = true; flushSync();
		expect(view().state.readOnly).toBe(false);
		props.covered = false; props.offScreen = false; flushSync();
		expect(view().state.readOnly).toBe(false);
		props.covered = true; props.offScreen = true; flushSync();
		vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
		document.dispatchEvent(new Event('visibilitychange')); flushSync();
		props.covered = false; props.offScreen = false; flushSync(); assertReading();
	});
	it('protects live undo/redo in Reading without consuming their history', async () => {
		await open(); enter();
		const editor = view();
		editor.dispatch({ changes: { from: 0, insert: 'local ' }, userEvent: 'input.type' }); flushSync();
		const press = (redo = false) => editor.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: redo ? 'y' : 'z', code: redo ? 'KeyY' : 'KeyZ', ctrlKey: true, bubbles: true, cancelable: true }));
		click('Done'); editor.contentDOM.focus(); press(); press(true);
		expect(editor.state.doc.toString()).toBe('local ' + DOC);
		expect(get(liveDocumentBindings)[target.instanceId].text.toString()).toBe('local ' + DOC);
		enter(); press(); flushSync(); expect(editor.state.doc.toString()).toBe(DOC);
		click('Done'); editor.contentDOM.focus(); press(true);
		expect(editor.state.doc.toString()).toBe(DOC);
		enter(); press(true); flushSync(); expect(editor.state.doc.toString()).toBe('local ' + DOC);
	});
	it('keeps existing local editing offline and returns to protected Reading', async () => {
		await open(); connectionState.set('disconnected'); flushSync(); enter();
		expect(view().state.readOnly).toBe(false);
		view().dispatch({ changes: { from: 0, insert: 'offline ' }, userEvent: 'input.type' }); flushSync();
		click('Done'); assertReading('offline ' + DOC);
		expect(existingDraft(target)?.editText).toBe('offline ' + DOC);
	});
	it.each(['cancel', 'outside', 'focus-moved'] as const)('keeps Reading protected after actual comment %s and clears the old selection on next Edit', async close => {
		await open(); view().dispatch({ selection: { anchor: 0, head: 5 } }); flushSync();
		host.querySelector<HTMLButtonElement>('.touch-comment-bar button')!.click(); flushSync();
		await vi.waitFor(() => expect(document.querySelector('.composer textarea')).not.toBeNull());
		if (close === 'cancel') document.querySelector<HTMLButtonElement>('.composer .cancel')!.click();
		else {
			if (close === 'focus-moved') { const field = host.appendChild(document.createElement('input')); field.focus(); }
			document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		}
		flushSync(); assertReading(); enter();
		expect(view().state.selection.main.empty).toBe(true);
		expect(view().state.doc.toString()).toBe(DOC);
	});
	it('preserves the real comment draft quote, source range, body and focused field across Edit and Done', async () => {
		await open();
		view().dispatch({ selection: { anchor: 0, head: 5 } }); flushSync();
		const offer = host.querySelector<HTMLButtonElement>('.touch-comment-bar button')!;
		expect(offer.disabled).toBe(false); offer.click(); flushSync();
		const input = await vi.waitFor(() => {
			const field = document.querySelector<HTMLTextAreaElement>('.composer textarea'); if (!field) throw new Error('no draft field'); return field;
		});
		input.value = 'Keep this thought'; input.dispatchEvent(new Event('input', { bubbles: true })); input.focus(); flushSync();
		const before = get(commentDraft)!;
		const range = selectionRange(before.selection!);
		enter();
		expect(document.activeElement).toBe(input);
		expect(get(commentDraft)?.body).toBe('Keep this thought');
		expect(selectionRange(get(commentDraft)!.selection!)).toEqual(range);
		click('Done');
		expect(document.activeElement).toBe(input);
		expect(input.value).toBe('Keep this thought');
		expect(selectionRange(get(commentDraft)!.selection!)).toEqual(range);
		assertReading();
	});
});
