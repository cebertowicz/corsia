import { afterEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { get } from 'svelte/store';
import { connectionState } from './connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from './documentWire';
import { DocOpenError } from '$lib/docOpenRefusal';
import { closeLiveDocument, LIVE_OPEN_WAIT_MS, liveDocumentBindings, openLiveDocument, startLiveDocuments } from './liveDocuments';

const device = vi.hoisted(() => ({
	create: vi.fn(), destroy: vi.fn(async () => {}), ready: Promise.resolve(),
}));
vi.mock('./deviceDocument', () => ({ DeviceDocument: class {
	ready = device.ready; saved = true; error = '';
	constructor() { device.create(); }
	destroy = device.destroy;
	subscribe(listener: (value: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('./deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, unknown>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
const target = { instanceId: 'opening', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let stop: (() => void) | null = null;
let release: (() => void) | null = null;

function begin() {
	connectionState.set('connected'); stop = startLiveDocuments();
	const outcome = openLiveDocument(target).then(value => ({ value }), error => ({ error }));
	return outcome;
}
function reply() {
	const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	const server = new Y.Doc(); server.getText('text').insert(0, 'server text');
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)) });
	server.destroy();
}
async function settled(outcome: Promise<unknown>) {
	return Promise.race([outcome, new Promise(resolve => setTimeout(() => resolve('still pending'), 100))]);
}
afterEach(async () => {
	vi.useRealTimers();
	release?.(); release = null; await Promise.resolve();
	stop?.(); stop = null; await Promise.resolve();
	wire.frames.length = 0; device.create.mockReset(); device.destroy.mockClear(); device.ready = Promise.resolve();
	vi.restoreAllMocks();
});

it('curates a typed setup-open refusal before returning its public error', async () => {
	const outcome = begin();
	const request = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	emit('doc_error', { type: 'doc_error', request_id: request.request_id, kind: 'setup_failed', code: 'setup_failed',
		title: '', message: 'Document setup failed; open it again to retry (private fixture reason)' });
	const result = await settled(outcome) as { error: DocOpenError };
	expect(result.error).toBeInstanceOf(DocOpenError);
	expect(result.error.code).toBe('setup_failed');
	expect(result.error.message).toBe("Live editing couldn't start for this document.");
	expect(device.create).not.toHaveBeenCalled();
});

it('rejects an opening when its device constructor throws', async () => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	device.create.mockImplementationOnce(() => { throw new Error('device constructor refused'); });
	const outcome = begin(); reply();
	expect(await settled(outcome)).toEqual({ error: expect.objectContaining({ message: 'device constructor refused' }) });
	expect(get(liveDocumentBindings)).toEqual({});
});

it('settles a closed opening before the response arrives', async () => {
	const outcome = begin(); closeLiveDocument(target.instanceId); reply();
	expect(await settled(outcome)).toEqual({ error: expect.objectContaining({ message: expect.stringContaining('closed') }) });
	expect(device.create).not.toHaveBeenCalled();
	expect(get(liveDocumentBindings)).toEqual({});
});

it('destroys a session whose binding closes while device loading is pending', async () => {
	device.ready = new Promise(resolve => { release = resolve; });
	const outcome = begin(); reply();
	await vi.waitFor(() => expect(device.create).toHaveBeenCalledOnce());
	closeLiveDocument(target.instanceId);
	release!();
	expect(await settled(outcome)).toEqual({ error: expect.objectContaining({ message: expect.stringContaining('closed') }) });
	await vi.waitFor(() => expect(device.destroy).toHaveBeenCalledOnce());
	expect(get(liveDocumentBindings)).toEqual({});
	expect(wire.frames.filter(frame => frame.type === 'doc_update')).toEqual([]);
});

it('releases device ownership on close even when loading never finishes', async () => {
	device.ready = new Promise(resolve => { release = resolve; });
	const outcome = begin(); reply();
	await vi.waitFor(() => expect(device.create).toHaveBeenCalledOnce());
	closeLiveDocument(target.instanceId);
	expect(await settled(outcome)).toEqual({ error: expect.any(Error) });
	await vi.waitFor(() => expect(device.destroy).toHaveBeenCalledOnce(), { timeout: 100 });
	device.ready = Promise.resolve();
	const next = openLiveDocument({ ...target, instanceId: 'next' }); reply();
	expect(await settled(next)).toEqual(expect.objectContaining({ artifactId: 'artifact' }));
});

it('waits for the socket before opening and opens once it connects', async () => {
	connectionState.set('connecting'); stop = startLiveDocuments();
	const outcome = openLiveDocument(target).then(value => ({ value }), error => ({ error }));
	expect(await settled(outcome)).toBe('still pending');
	expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
	connectionState.set('connected');
	expect(wire.frames.filter(frame => frame.type === 'doc_open')).toHaveLength(1);
	reply();
	expect(await settled(outcome)).toEqual({ value: expect.objectContaining({ artifactId: 'artifact' }) });
});

it('re-sends an opening interrupted by a disconnect and ignores the stale answer', async () => {
	const outcome = begin();
	const first = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	connectionState.set('connecting');
	expect(await settled(outcome)).toBe('still pending');
	connectionState.set('connected');
	const second = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	expect(second.request_id).not.toBe(first.request_id);
	emit('doc_opened', { type: 'doc_opened', live: false, request_id: first.request_id });
	expect(await settled(outcome)).toBe('still pending');
	reply();
	expect(await settled(outcome)).toEqual({ value: expect.objectContaining({ artifactId: 'artifact' }) });
});

it('refuses a waiting opening as not connected once the bound passes, and sends nothing', async () => {
	expect(LIVE_OPEN_WAIT_MS).toBe(30_000);
	vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
	connectionState.set('connecting'); stop = startLiveDocuments();
	let result: unknown = 'still pending';
	void openLiveDocument(target).then(value => { result = { value }; }, error => { result = { error }; });
	vi.advanceTimersByTime(LIVE_OPEN_WAIT_MS - 1); await Promise.resolve();
	expect(result).toBe('still pending');
	vi.advanceTimersByTime(1); await Promise.resolve(); await Promise.resolve();
	expect(result).toEqual({ error: expect.any(DocOpenError) });
	expect((result as { error: DocOpenError }).error.code).toBe('not_connected');
	connectionState.set('connected');
	expect(wire.frames.filter(frame => frame.type === 'doc_open')).toEqual([]);
});
