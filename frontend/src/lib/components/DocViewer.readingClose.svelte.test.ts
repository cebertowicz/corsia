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
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { discardCommentDraft } from '$lib/stores/commentDraft';
import { closeArtifactTab, openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { requestArtifactTabClose } from '$lib/artifactTabClose';
import type { ArtifactTab } from '$lib/artifactTabTypes';
import type { DocViewerTarget } from '$lib/docViewerTarget';

// The actual artifactWorkspace tab producer and its one explicit-close path (requestArtifactTabClose) drive a real
// DocViewer; only the external save answers, the comment POST and the feedback POST are stubbed.
const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async original => ({ ...await original<typeof import('$lib/api/docs')>(), ...api }));
const posts = vi.hoisted(() => ({ addComment: vi.fn(), sendDocFeedback: vi.fn() }));
vi.mock('$lib/stores/docComments', async original => ({ ...await original<typeof import('$lib/stores/docComments')>(), addComment: posts.addComment }));
vi.mock('$lib/api/docFeedback', async original => ({ ...await original<typeof import('$lib/api/docFeedback')>(), sendDocFeedback: posts.sendDocFeedback }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

type Save = 'clean' | 'pending' | 'failed';
const THREAD = 'close-thread', DOC = 'intro\n\n| Item | Owner |\n|---|---|\n| Alpha | Dana |\n\nafter\n';
let host: HTMLElement, component: ReturnType<typeof mount> | undefined, bar: ReturnType<typeof mount> | undefined;
let stop: () => void, tab: ArtifactTab & { kind: 'document' }, target: DocViewerTarget;
beforeEach(() => {
	vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query === '(pointer: coarse)', media: query, addEventListener() {}, removeEventListener() {} })));
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
	Range.prototype.getBoundingClientRect = () => new DOMRect();
	HTMLElement.prototype.scrollIntoView = vi.fn();
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue({ left: 20, right: 30, top: 20, bottom: 30 });
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
	posts.sendDocFeedback.mockResolvedValue({ woken: [], undelivered_batches: [], open_count: 0, document_held_count: 0, image_held_count: 0 });
});
afterEach(async () => {
	if (component) await unmount(component);
	if (bar) await unmount(bar);
	component = undefined; bar = undefined;
	for (const open of get(threadWorkspaces)[THREAD]?.tabs ?? []) closeArtifactTab(THREAD, open.instanceId);
	host?.remove(); stop(); clearDraft(target); discardCommentDraft(); wire.frames.length = 0;
	document.getSelection()?.removeAllRanges();
	vi.restoreAllMocks(); vi.unstubAllGlobals();
	for (const fn of [...Object.values(api), ...Object.values(posts)]) fn.mockReset();
});
/** Opens the document through the real workspace store, then mounts the viewer on that tab's own target. */
async function openDocument(text: string) {
	api.fetchContent.mockResolvedValue({ text, writable: true, artifact: { id: 'artifact' }, freshness: null });
	openArtifactTab(THREAD, { artifactId: 'artifact', path: '/documents/read.md', title: 'read.md' });
	tab = get(threadWorkspaces)[THREAD].tabs.at(-1) as typeof tab;
	target = { instanceId: tab.instanceId, path: tab.path, name: tab.title, projectId: null, threadId: THREAD, artifactId: 'artifact' };
	const from = wire.frames.length;
	host = document.body.appendChild(document.createElement('div'));
	component = mount(DocViewer, { target: host, props: { target, offScreen: false, covered: false } });
	bar = mount(DocumentBarTestHost, { target: host }); flushSync();
	await vi.waitFor(() => expect(wire.frames.slice(from).some(frame => frame.type === 'doc_open')).toBe(true));
	const server = new Y.Doc(); server.getText('text').insert(0, text);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: wire.frames.slice(from).findLast(frame => frame.type === 'doc_open')!.request_id,
		update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('intro'));
	flushSync();
}
async function closeViewer() {
	if (component) await unmount(component);
	if (bar) await unmount(bar);
	component = undefined; bar = undefined; host.remove();
}
const view = () => EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
function click(text: string) {
	[...host.querySelectorAll('button')].find(el => el.textContent?.trim() === text)!.click(); flushSync();
}
function enter() { host.querySelector<HTMLButtonElement>('[data-document-details]')!.click(); flushSync(); click('Edit'); }
function assertReading(expected: string) {
	expect(view().state.facet(EditorView.editable)).toBe(false);
	expect(view().state.readOnly).toBe(true);
	view().dispatch({ selection: { anchor: 0, head: 5 } });
	deleteCharBackward(view());
	expect(view().state.doc.toString()).toBe(expected);
}
const flushes = () => wire.frames.filter(frame => frame.type === 'doc_flush').length;
const acks = () => emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
async function answerLatest(ok: boolean, text: string) {
	const latest = wire.frames.findLast(frame => frame.type === 'doc_flush')!;
	if (!ok) return emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: latest.request_id, kind: 'file_write_failed', message: 'Write refused for this test' });
	acks();
	emit('doc_saved', { type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: latest.request_id, server_saved: true, pending: false, held: null, digest: await textDigest(text) });
}
/** Types in Edit, then leaves the save clean (answered), pending (unanswered) or failed (refused). */
async function writeThenSave(save: Save) {
	enter();
	view().dispatch({ changes: { from: DOC.length, insert: 'local' }, userEvent: 'input.type' }); flushSync();
	const edited = view().state.doc.toString();
	const saving = flushDraft(target);
	await vi.waitFor(() => expect(flushes()).toBeGreaterThan(0));
	// The server holds the update in memory (acked) whatever happens to the file write.
	acks();
	if (save !== 'pending') await answerLatest(save === 'clean', edited);
	if (save === 'failed') expect(await saving).toBe(false);
	if (save === 'clean') expect(await saving).toBe(true);
	flushSync();
	return edited;
}
const tabOpen = () => get(threadWorkspaces)[THREAD].tabs.some(open => open.instanceId === tab.instanceId);

describe('actual tab close and reopen of a touch-opened document', () => {
	it.each(['clean', 'pending'] as const)('closes with %s save through the existing gate and reopens in Reading with the saved writing', async save => {
		await openDocument(DOC);
		const edited = await writeThenSave(save);
		const closing = requestArtifactTabClose(THREAD, tab, vi.fn());
		if (save === 'pending') { await Promise.resolve(); await answerLatest(true, edited); }
		await closing;
		expect(tabOpen()).toBe(false);
		expect(existingDraft(target)).toBeUndefined();
		expect(wire.frames.some(frame => frame.type === 'doc_close')).toBe(true);
		await closeViewer();
		await openDocument(edited);
		expect(host.querySelector('.bar-done')).toBeNull();
		assertReading(edited);
		expect(existingDraft(target)?.editText ?? edited).toBe(edited);
	});
	it('keeps a failed save open in view, retains the writing and error, then closes after a retry and reopens in Reading', async () => {
		await openDocument(DOC);
		const edited = await writeThenSave('failed');
		const bring = vi.fn(), before = flushes();
		const refused = requestArtifactTabClose(THREAD, tab, bring);
		await vi.waitFor(() => expect(flushes()).toBeGreaterThan(before));
		await answerLatest(false, edited); await refused;
		expect(bring).toHaveBeenCalledTimes(1);
		expect(tabOpen()).toBe(true);
		expect(existingDraft(target)).toMatchObject({ editText: edited, status: 'error' });
		expect(view().state.doc.toString()).toBe(edited);
		const retrying = requestArtifactTabClose(THREAD, tab, bring);
		await vi.waitFor(() => expect(flushes()).toBeGreaterThan(before + 1));
		await answerLatest(true, edited); await retrying;
		expect(tabOpen()).toBe(false);
		await closeViewer();
		await openDocument(edited);
		assertReading(edited);
		expect(host.querySelector('.bar-done')).toBeNull();
	});
});
describe('actual posted comment after Done', () => {
	it.each(['clean', 'pending', 'failed'] as const)('keeps Reading protected, text and %s save state after the comment posts, and clears the old selection for next Edit', async save => {
		posts.addComment.mockResolvedValue('comment-1');
		await openDocument(DOC);
		const edited = await writeThenSave(save);
		click('Done');
		const status = { status: existingDraft(target)?.status, error: existingDraft(target)?.error };
		view().dispatch({ selection: { anchor: 0, head: 5 } }); flushSync();
		host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
		const field = await vi.waitFor(() => { const el = document.querySelector<HTMLTextAreaElement>('.composer textarea'); if (!el) throw new Error('no composer'); return el; });
		field.value = 'A posted thought'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		document.querySelector<HTMLButtonElement>('.composer .submit')!.click(); flushSync();
		// The server reserves the range, the one answer a posted live comment waits for.
		const ask = await vi.waitFor(() => { const frame = wire.frames.findLast(item => item.type === 'doc_positions'); if (!frame) throw new Error('no reservation ask'); return frame; });
		emit('doc_positions', { type: 'doc_positions', artifact_id: 'artifact', store_id: 'store', request_id: ask.request_id, seq: ask.seq, reserved: true });
		await vi.waitFor(() => expect(posts.addComment).toHaveBeenCalledTimes(1));
		expect(posts.addComment.mock.calls[0].slice(0, 5)).toEqual(['artifact', 0, 5, 'intro', 'A posted thought']);
		await vi.waitFor(() => expect(document.querySelector('.composer textarea')).toBeNull());
		assertReading(edited);
		expect({ status: existingDraft(target)?.status, error: existingDraft(target)?.error }).toEqual(status);
		enter();
		expect(view().state.selection.main.empty).toBe(true);
		expect(view().state.doc.toString()).toBe(edited);
	});
});
describe('actual comment closes after Done, crossed with save state', () => {
	it.each(['cancel', 'outside', 'focus-moved'].flatMap(close => (['clean', 'pending', 'failed'] as const).map(save => ({ close, save }))))('keeps Reading protected after $close with $save save and clears the old selection for next Edit', async ({ close, save }) => {
		await openDocument(DOC);
		const edited = await writeThenSave(save);
		click('Done');
		const status = { status: existingDraft(target)?.status, error: existingDraft(target)?.error };
		view().dispatch({ selection: { anchor: 0, head: 5 } }); flushSync();
		host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
		await vi.waitFor(() => expect(document.querySelector('.composer textarea')).not.toBeNull());
		if (close === 'cancel') document.querySelector<HTMLButtonElement>('.composer .cancel')!.click();
		else {
			if (close === 'focus-moved') host.appendChild(document.createElement('input')).focus();
			document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		}
		flushSync();
		assertReading(edited);
		expect({ status: existingDraft(target)?.status, error: existingDraft(target)?.error }).toEqual(status);
		enter();
		expect(view().state.selection.main.empty).toBe(true);
		expect(view().state.doc.toString()).toBe(edited);
	});
});
