import { afterEach, describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import { connectionState } from './connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from './documentWire';
import { closeLiveDocument, liveDocumentBindings, openLiveDocument, startLiveDocuments } from './liveDocuments';
import type { LiveDocumentSession } from './liveDocumentSession';

const copies = vi.hoisted(() => ({ created: vi.fn(), destroy: vi.fn(async () => {}), recover: vi.fn(async () => []) }));
vi.mock('./deviceDocument', () => ({ DeviceDocument: class {
	constructor() { copies.created(); }
	ready = Promise.resolve(); saved = true; error = '';
	destroy = copies.destroy;
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('./deviceRecovery', () => ({ readDeviceRecovery: copies.recover }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
const target = (instanceId: string) => ({ instanceId, path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' });
let stop: (() => void) | null = null;
async function open(instanceId: string, storeId = 'store') {
	const promise = openLiveDocument(target(instanceId));
	const request = wire.frames.at(-1)!;
	const server = new Y.Doc(); server.getText('text').insert(0, 'server text');
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: storeId,
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)) });
	// A live answer resolves a session.
	return promise as Promise<LiveDocumentSession>;
}
afterEach(() => { stop?.(); stop = null; wire.frames.length = 0; copies.recover.mockReset().mockResolvedValue([]); copies.destroy.mockClear(); copies.created.mockClear(); });

describe('live document ownership', () => {
	it('shares a document between app tabs and only closes the last binding', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const first = await open('one'); const second = await open('two');
		expect(first).toBe(second);
		closeLiveDocument('one');
		expect(wire.frames.filter(frame => frame.type === 'doc_close')).toEqual([]);
		closeLiveDocument('two');
		expect(wire.frames.at(-1)?.type).toBe('doc_close');
		expect(get(liveDocumentBindings)).toEqual({});
		expect(copies.destroy).toHaveBeenCalledOnce();
	});

	it('keeps unsent tab text separate when a reconnect reports a rebuilt store', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const first = (await open('one'))!;
		connectionState.set('disconnected');
		first.text.insert(0, 'LOCAL ');
		connectionState.set('connected');
		const request = wire.frames.at(-1)!;
		const server = new Y.Doc(); server.getText('text').insert(0, 'reset text');
		emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'reset',
			request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)) });
		await vi.waitFor(() => expect(get(liveDocumentBindings).one?.storeId).toBe('reset'));
		const rebuilt = get(liveDocumentBindings).one;
		expect(rebuilt.text.toString()).toBe('reset text');
		expect(rebuilt.recovery).toEqual([{ storeId: 'store', text: 'LOCAL server text' }]);
	});

	it('reconciles missed owner clears only on a matched same-store reconnect', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const session = await open('one');
		emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'store', kind: 'publication_failed', blocking: true, failure_generation: 3 });
		emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'store', kind: 'store_refused', blocking: true, failure_generation: 8, message: 'disk fixture' });
		await vi.waitFor(() => expect(session.blockingFailures).toHaveLength(2));
		connectionState.set('disconnected'); session.text.insert(0, 'LOCAL ');
		const local = session.text.toString();
		connectionState.set('connected');
		const request = wire.frames.at(-1)!;
		const server = new Y.Doc(); server.getText('text').insert(0, 'server text');
		const admission = { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
			request_id: 'wrong', update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)),
			setup_incarnation: 'new-manager', failure_clears: [{ kind: 'publication_failed', clears_through: 3 }, { kind: 'store_refused', clears_through: 7 }] };
		emit('doc_opened', admission);
		expect(session.blockingFailures).toHaveLength(2);
		emit('doc_opened', { ...admission, request_id: request.request_id });
		await vi.waitFor(() => expect(session.connection).toBe('connected'));
		expect(get(liveDocumentBindings).one).toBe(session);
		expect(session.blockingFailures.map(row => row.kind)).toEqual(['store_refused']);
		expect(session.text.toString()).toContain('LOCAL ');
		expect(session.text.toString()).toContain(local);
		expect(session.durability.clean).toBe(false);
	});

	it('takes the legacy route without creating a device copy when live opening is excluded', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const promise = openLiveDocument(target('guidance'));
		emit('doc_opened', { type: 'doc_opened', live: false, request_id: wire.frames.at(-1)!.request_id });
		expect(await promise).toBeNull();
		expect(get(liveDocumentBindings)).toEqual({});
		expect(copies.recover).not.toHaveBeenCalled();
	});

	it('answers a damaged store as non-live with its reason, never reading, merging or deleting the device copy', async () => {
		connectionState.set('connected'); stop = startLiveDocuments();
		const promise = openLiveDocument(target('damaged'));
		emit('doc_opened', { type: 'doc_opened', live: false, live_unavailable: 'store_damaged', request_id: wire.frames.at(-1)!.request_id });
		expect(await promise).toBe('store_damaged');
		expect(get(liveDocumentBindings)).toEqual({});
		expect(copies.created).not.toHaveBeenCalled();
		expect(copies.recover).not.toHaveBeenCalled();
		expect(copies.destroy).not.toHaveBeenCalled();
	});
});
