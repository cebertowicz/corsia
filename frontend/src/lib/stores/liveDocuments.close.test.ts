import { afterEach, describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import { connectionState } from './connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate, textDigest } from './documentWire';
import { liveDocumentBindings, openLiveDocument, startLiveDocuments } from './liveDocuments';
import type { LiveDocumentSession } from './liveDocumentSession';
import { closeTabWithSaveGate, seedDraft } from './docSaveQueue';
import { closeArtifactTab, openArtifactTab, resolveArtifactTab, setActiveTab, threadWorkspaces } from './artifactWorkspace';

const copies = vi.hoisted(() => ({ destroy: vi.fn(async () => {}), clearData: vi.fn(async () => {}) }));
vi.mock('./deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	persistence = { clearData: copies.clearData };
	destroy = copies.destroy;
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('./deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
vi.mock('$lib/api/docs', () => ({ writeContent: vi.fn() }));
vi.mock('./docComments', () => ({ flushPositions: vi.fn() }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));

const THREAD = 'close-thread', OTHER = 'other-thread';
let stop: (() => void) | null = null;

/** Opens a document tab and its live binding under that tab's instance; a
 * `path` opens a tab that has not resolved its artifact id yet. */
async function openTab(thread = THREAD, path?: string) {
	openArtifactTab(thread, path ? { path, title: 'doc.md' } : { artifactId: 'artifact', path: '/documents/doc.md', title: 'doc.md' });
	const tab = get(threadWorkspaces)[thread].tabs.at(-1)!;
	const target = { instanceId: tab.instanceId, path: tab.path, name: 'doc.md', projectId: null, threadId: thread, artifactId: 'artifact' };
	const opening = openLiveDocument(target);
	const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	const server = new Y.Doc(); server.getText('text').insert(0, 'server text');
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)) });
	server.destroy();
	// A live answer resolves a session.
	const session = (await opening) as LiveDocumentSession;
	seedDraft(target, session.text.toString());
	return { tab, target, session };
}

/** Answers the close gate's one flush: the lane acks what it received, then the flush lands. */
async function answerFlush() {
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_flush')).toBe(true));
	const flush = wire.frames.findLast(frame => frame.type === 'doc_flush')!;
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store',
		message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
	emit('doc_saved', { type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: flush.request_id,
		server_saved: true, pending: false, held: null, digest: await textDigest('server text') });
}

const after = (type: string) => wire.frames.slice(wire.frames.findLastIndex(frame => frame.type === type) + 1);

afterEach(() => {
	stop?.(); stop = null;
	threadWorkspaces.set({});
	wire.frames.length = 0;
	copies.destroy.mockClear(); copies.clearData.mockClear();
});

describe('closing a document tab closes its live binding', () => {
	it('sends the close sync, then doc_close, never waits on their ack, and keeps the device copy', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const { tab, target } = await openTab();
		const onClose = vi.fn();
		const closing = closeTabWithSaveGate(target, { onClose, onBringIntoView: vi.fn() });
		await answerFlush();
		// Settled by the flush answer alone: nothing acknowledges the close sync or doc_close.
		expect(await closing).toEqual({ closed: true });
		expect(after('doc_flush').map(frame => frame.type)).toEqual(['doc_sync', 'doc_close']);
		expect(get(liveDocumentBindings)[tab.instanceId]).toBeUndefined();
		expect(copies.destroy).toHaveBeenCalledOnce();
		expect(copies.clearData).not.toHaveBeenCalled();
	});

	it('keeps a second tab bound when the first closes, keeps bindings across tab switches, and the last close reaches the server', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const first = await openTab(THREAD);
		const second = await openTab(OTHER);
		expect(second.session).toBe(first.session);
		openArtifactTab(THREAD, { path: '/documents/notes.md', title: 'notes.md' });
		setActiveTab(THREAD, first.tab.instanceId);
		expect(Object.keys(get(liveDocumentBindings)).sort()).toEqual([first.tab.instanceId, second.tab.instanceId].sort());
		const closing = closeTabWithSaveGate(first.target, { onClose: vi.fn(), onBringIntoView: vi.fn() });
		await answerFlush();
		expect(await closing).toEqual({ closed: true });
		closeArtifactTab(THREAD, first.tab.instanceId);
		expect(get(liveDocumentBindings)[first.tab.instanceId]).toBeUndefined();
		expect(get(liveDocumentBindings)[second.tab.instanceId]).toBe(second.session);
		expect(wire.frames.filter(frame => frame.type === 'doc_close')).toEqual([]);
		expect(copies.destroy).not.toHaveBeenCalled();
		closeArtifactTab(OTHER, second.tab.instanceId);
		expect(get(liveDocumentBindings)).toEqual({});
		expect(wire.frames.filter(frame => frame.type === 'doc_close')).toEqual([{ type: 'doc_close', artifact_id: 'artifact', store_id: 'store' }]);
		expect(copies.clearData).not.toHaveBeenCalled();
	});

	it('a duplicate tab dropped by a merge closes its binding and the survivor keeps the session', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const survivor = await openTab(THREAD);
		const duplicate = await openTab(THREAD, '/documents/alias.md');
		expect(duplicate.session).toBe(survivor.session);
		resolveArtifactTab(THREAD, duplicate.tab.instanceId, 'artifact');
		expect(get(threadWorkspaces)[THREAD].tabs.map(tab => tab.instanceId)).toEqual([survivor.tab.instanceId]);
		expect(Object.keys(get(liveDocumentBindings))).toEqual([survivor.tab.instanceId]);
		expect(wire.frames.filter(frame => frame.type === 'doc_close')).toEqual([]);
		expect(copies.destroy).not.toHaveBeenCalled();
	});

	it('a merge that drops the last bound tab closes the session', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		openArtifactTab(THREAD, { artifactId: 'artifact', path: '/documents/doc.md', title: 'doc.md' });
		const duplicate = await openTab(THREAD, '/documents/alias.md');
		resolveArtifactTab(THREAD, duplicate.tab.instanceId, 'artifact');
		expect(get(threadWorkspaces)[THREAD].tabs).toHaveLength(1);
		expect(get(liveDocumentBindings)).toEqual({});
		expect(wire.frames.filter(frame => frame.type === 'doc_close')).toEqual([{ type: 'doc_close', artifact_id: 'artifact', store_id: 'store' }]);
		expect(copies.destroy).toHaveBeenCalledOnce();
		expect(copies.clearData).not.toHaveBeenCalled();
	});
});
