import { afterEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';
import { clearDraft, existingDraft, seedDraft, setDraftText } from './docSaveQueue';

const calls = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock('$lib/api/docs', () => ({ writeContent: vi.fn() }));
vi.mock('./docComments', () => ({ flushPositions: vi.fn() }));
vi.mock('./toast', () => ({ toasts: { show: calls.toast } }));
const target = { instanceId: 'busy', path: '/doc.md', name: 'doc.md', artifactId: 'artifact', projectId: null, threadId: null };
let session: LiveDocumentSession;
const frames: Record<string, any>[] = [];
const flushes = () => frames.filter(frame => frame.type === 'doc_flush');
function setup() {
	vi.useFakeTimers();
	const doc = new Y.Doc(); doc.getText('text').insert(0, 'base');
	session = new LiveDocumentSession({ doc, artifactId: 'artifact', storeId: 'store', writable: true, send: frame => { frames.push(frame); return true; } });
	session.connection = 'connected'; liveDocumentBindings.set({ busy: session }); seedDraft(target, 'base');
	session.text.insert(0, 'typed '); setDraftText(target, 'typed base');
}
function busy() {
	const request = flushes().at(-1)!;
	session.handle({ type: 'doc_error', artifact_id: 'artifact', request_id: request.request_id, kind: 'store_refused', code: 'history_lock_busy', blocking: true, message: 'The document is busy. Try saving again.' });
}
async function saved() {
	const request = flushes().at(-1)!;
	session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
	session.handle({ type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id, server_saved: true });
	await vi.advanceTimersByTimeAsync(0);
}
afterEach(() => { clearDraft(target); liveDocumentBindings.set({}); session?.destroy(); session?.doc.destroy(); frames.length = 0; vi.clearAllMocks(); vi.useRealTimers(); });

it('retries a busy flush once without showing an error when it succeeds', async () => {
	setup(); await vi.advanceTimersByTimeAsync(700); expect(flushes()).toHaveLength(1); busy();
	await vi.advanceTimersByTimeAsync(0); expect(session.failureLines).toEqual([]); expect(calls.toast).not.toHaveBeenCalled();
	await vi.advanceTimersByTimeAsync(1000); expect(flushes()).toHaveLength(2); await saved();
	expect(existingDraft(target)).toMatchObject({ status: 'saved', savedText: 'typed base', error: '' });
	expect(session.failureLines).toEqual([]); expect(calls.toast).not.toHaveBeenCalled();
});

it('shows busy after two automatic attempts fail and keeps retrying with backoff', async () => {
	setup(); await vi.advanceTimersByTimeAsync(700); busy(); await vi.advanceTimersByTimeAsync(1000); busy();
	await vi.advanceTimersByTimeAsync(2000); busy(); await vi.advanceTimersByTimeAsync(0);
	expect(session.failureLines).toContain('Not saved to the file: The document is busy. Try saving again.');
	expect(calls.toast).toHaveBeenCalledTimes(1);
	await vi.advanceTimersByTimeAsync(4000); expect(flushes()).toHaveLength(4); busy();
	await vi.advanceTimersByTimeAsync(0); expect(existingDraft(target)?.status).toBe('error');
	await vi.advanceTimersByTimeAsync(8000); expect(flushes()).toHaveLength(5); await saved();
	expect(session.failureLines).toEqual([]); expect(existingDraft(target)?.status).toBe('saved');
});

it('marks a successful Retry as written and clears the draft error', async () => {
	setup(); await vi.advanceTimersByTimeAsync(700); busy(); await vi.advanceTimersByTimeAsync(1000); busy();
	await vi.advanceTimersByTimeAsync(2000); busy(); await vi.advanceTimersByTimeAsync(0);
	const retry = session.retry(); await saved(); await retry; await vi.advanceTimersByTimeAsync(0);
	expect(session.durability.saved).toBe(true); expect(existingDraft(target)).toMatchObject({ status: 'saved', error: '' });
});

it('stops retrying after the tab draft is cleared', async () => {
	setup(); await vi.advanceTimersByTimeAsync(700); busy(); await vi.advanceTimersByTimeAsync(0);
	clearDraft(target); await vi.advanceTimersByTimeAsync(10000);
	expect(flushes()).toHaveLength(1);
});

it('keeps the backoff and visible error while the person continues typing', async () => {
	setup(); await vi.advanceTimersByTimeAsync(700); busy(); await vi.advanceTimersByTimeAsync(1000); busy();
	await vi.advanceTimersByTimeAsync(2000); busy(); await vi.advanceTimersByTimeAsync(0);
	session.text.insert(0, 'more '); setDraftText(target, 'more typed base');
	await vi.advanceTimersByTimeAsync(700);
	expect(flushes()).toHaveLength(3);
	expect(existingDraft(target)?.status).toBe('error');
	await vi.advanceTimersByTimeAsync(3300); expect(flushes()).toHaveLength(4);
	await saved(); expect(existingDraft(target)).toMatchObject({ status: 'saved', savedText: 'more typed base', error: '' });
});
