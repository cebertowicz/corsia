import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate, textDigest } from '$lib/stores/documentWire';
import { liveDocumentBindings, startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, existingDraft } from '$lib/stores/docSaveQueue';
import type { DocViewerTarget } from '$lib/docViewerTarget';

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

const notes: DocViewerTarget = { instanceId: 'tab-notes', path: '/documents/notes.md', name: 'notes.md', projectId: 'project', threadId: 'thread', artifactId: 'notes' };
const other: DocViewerTarget = { instanceId: 'tab-other', path: '/documents/other.md', name: 'other.md', projectId: 'project', threadId: 'thread', artifactId: 'other' };
const texts: Record<string, string> = { notes: 'notes text', other: 'other text' };
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
const docs: Y.Doc[] = [];
const flushes = () => wire.frames.filter(frame => frame.type === 'doc_flush');
const ghost = () => host.querySelector('.save-ghost')?.textContent?.trim() ?? '';

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchContent.mockImplementation(async (path: string) => {
		const id = path.includes('notes') ? 'notes' : 'other';
		return { text: texts[id], writable: true, artifact: { id }, freshness: null };
	});
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'notes', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	vi.useRealTimers();
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(notes); clearDraft(other);
	wire.frames.length = 0;
	for (const doc of docs.splice(0)) doc.destroy();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

/** Answers the newest open of `artifact` with its live document. */
async function serverOpen(artifact: string) {
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open' && frame.path.includes(artifact))).toBe(true));
	const request = wire.frames.findLast(frame => frame.type === 'doc_open' && frame.path.includes(artifact))!;
	wire.frames.splice(wire.frames.indexOf(request), 1);
	const server = new Y.Doc(); docs.push(server); server.getText('text').insert(0, texts[artifact]);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: artifact, store_id: `store-${artifact}`,
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(texts[artifact]));
	// The lane commits the open's sync and acks it, as it acks every message it receives.
	const synced = wire.frames.filter(frame => frame.type === 'doc_sync' && frame.artifact_id === artifact);
	expect(synced.length).toBeGreaterThan(0);
	emit('doc_ack', { type: 'doc_ack', artifact_id: artifact, store_id: `store-${artifact}`, message_ids: synced.map(frame => frame.message_id) });
}

describe('a return to a document tab with no edit', () => {
	it('writes nothing on the way out or back, and shows no Saved badge', async () => {
		const props = $state({ target: notes });
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(DocViewer, { target: host, props });
		flushSync();
		await serverOpen('notes');
		// From here a save's debounce timer is fake, so the waits past it below cost no wall-clock time.
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		props.target = other;
		flushSync();
		await serverOpen('other');
		// Leaving a tab with nothing typed sends it no save: no live flush, no whole-text write.
		await vi.advanceTimersByTimeAsync(800);
		expect(flushes()).toEqual([]);
		expect(api.writeContent).not.toHaveBeenCalled();
		expect(existingDraft(notes)?.status).toBe('idle');

		props.target = notes;
		flushSync();
		// The cached paint and the confirmed one both show no badge: nothing was saved.
		expect(host.querySelector('.cm-content')?.textContent).toBe(texts.notes);
		expect(ghost()).toBe('');
		await vi.waitFor(() => expect(EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!.state.readOnly).toBe(false));
		expect(ghost()).toBe('');
		await vi.advanceTimersByTimeAsync(800);
		expect(flushes()).toEqual([]);
		expect(api.writeContent).not.toHaveBeenCalled();
	});

	it('still saves a real edit on the way out, and its badge does not follow the tab back', async () => {
		const props = $state({ target: notes });
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(DocViewer, { target: host, props });
		flushSync();
		await serverOpen('notes');
		get(liveDocumentBindings)[notes.instanceId].text.insert(0, 'typed ');
		flushSync();
		props.target = other;
		flushSync();
		await vi.waitFor(() => expect(flushes()).toHaveLength(1));
		const request = flushes()[0];
		emit('doc_ack', { type: 'doc_ack', artifact_id: 'notes', store_id: 'store-notes',
			message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
		emit('doc_saved', { type: 'doc_saved', artifact_id: 'notes', store_id: 'store-notes', request_id: request.request_id,
			server_saved: true, pending: false, held: null, digest: await textDigest(`typed ${texts.notes}`) });
		await vi.waitFor(() => expect(existingDraft(notes)?.status).toBe('saved'));
		await serverOpen('other');

		props.target = notes;
		flushSync();
		expect(host.querySelector('.cm-content')?.textContent).toBe(`typed ${texts.notes}`);
		expect(ghost()).toBe('');
	});
});
