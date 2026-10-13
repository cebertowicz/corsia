import { afterEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';
import { clearDraft, closeTabWithSaveGate, existingDraft, flushDraft, seedDraft, setDraftText } from './docSaveQueue';
import { encodeUpdate, textDigest } from './documentWire';

const calls = vi.hoisted(() => ({ write: vi.fn().mockResolvedValue({ saved: true }), positions: vi.fn(), toast: vi.fn() }));
vi.mock('$lib/api/docs', () => ({ writeContent: calls.write }));
vi.mock('./docComments', () => ({ flushPositions: calls.positions }));
vi.mock('./toast', () => ({ toasts: { show: calls.toast } }));
const target = { instanceId: 'save-live', path: '/doc.md', name: 'doc.md', artifactId: 'artifact', projectId: null, threadId: null };
let session: LiveDocumentSession;
const frames: Record<string, any>[] = [];
function setup() {
	const doc = new Y.Doc(); doc.getText('text').insert(0, 'base');
	session = new LiveDocumentSession({ doc, artifactId: 'artifact', storeId: 'store', writable: true, send: frame => { frames.push(frame); return true; } });
	session.connection = 'connected';
	liveDocumentBindings.set({ [target.instanceId]: session });
	seedDraft(target, 'base');
}
afterEach(() => {
	clearDraft(target); liveDocumentBindings.set({});
	session?.destroy(); session?.doc.destroy(); frames.length = 0;
	vi.clearAllMocks();
});
function edit() { session.text.delete(0, 4); setDraftText(target, ''); }
const flushes = () => frames.filter(frame => frame.type === 'doc_flush');
const ack = (messages: Record<string, any>[]) => session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store',
	message_ids: messages.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
/** The lane's durable closure acks every batch it received before the flush lands, then the flush returns. */
async function complete(digest?: string) {
	await vi.waitFor(() => expect(flushes().length).toBeGreaterThan(0));
	const request = flushes().at(-1)!;
	ack(frames);
	session.handle({ type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id,
		server_saved: true, digest: digest ?? await textDigest(session.text.toString()), pending: false, held: null });
}
/** Another session appends text; the server forwards it, and the canvas shows it. */
function remote(text: string) {
	const other = new Y.Doc();
	Y.applyUpdate(other, Y.encodeStateAsUpdate(session.doc));
	const vector = Y.encodeStateVector(other);
	other.getText('text').insert(other.getText('text').length, text);
	session.handle({ type: 'doc_update', artifact_id: 'artifact', store_id: 'store', update: encodeUpdate(Y.encodeStateAsUpdate(other, vector)) });
	setDraftText(target, session.text.toString());
}
const within = <T>(promise: Promise<T>, ms = 300) => Promise.race([promise, new Promise(resolve => setTimeout(() => resolve('still open'), ms))]);
it('flushes a live deletion through the lane and never posts browser comment offsets', async () => {
	setup(); edit(); const flushing = flushDraft(target);
	await complete(); expect(await flushing).toBe(true);
	expect(existingDraft(target)).toMatchObject({ editText: '', savedText: '', status: 'saved' });
	expect(calls.write).not.toHaveBeenCalled(); expect(calls.positions).not.toHaveBeenCalled();
});
it('keeps the draft when a live flush refuses and closes only after an explicit successful retry', async () => {
	setup(); edit();
	const onClose = vi.fn(), onBringIntoView = vi.fn();
	const closing = closeTabWithSaveGate(target, { onClose, onBringIntoView });
	await vi.waitFor(() => expect(frames.some(frame => frame.type === 'doc_flush')).toBe(true));
	const request = frames.findLast(frame => frame.type === 'doc_flush')!;
	session.handle({ type: 'doc_error', artifact_id: 'artifact', request_id: request.request_id, message: 'disk refused' });
	expect(await closing).toEqual({ closed: false }); expect(onClose).not.toHaveBeenCalled();
	expect(existingDraft(target)?.editText).toBe(''); expect(calls.write).not.toHaveBeenCalled();
	frames.length = 0;
	const retried = closeTabWithSaveGate(target, { onClose, onBringIntoView });
	await complete(); expect(await retried).toEqual({ closed: true }); expect(onClose).toHaveBeenCalledOnce();
});
it.each(['publication_failed', 'setup_failed'])('curates %s before save queue error parsing and the toast', async kind => {
	setup(); session.text.insert(0, 'LOCAL '); setDraftText(target, session.text.toString());
	const text = session.text.toString(), onClose = vi.fn();
	const closing = closeTabWithSaveGate(target, { onClose, onBringIntoView: vi.fn() });
	await vi.waitFor(() => expect(flushes()).toHaveLength(1));
	session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: flushes()[0].request_id,
		kind, message: '{"detail":{"message":"private fixture path/reason"}}' });
	expect(await closing).toEqual({ closed: false });
	const expected = kind === 'publication_failed' ? "Live updates couldn't be shared for this document." : "Live editing couldn't start for this document.";
	expect(session.fileError).toBe(expected);
	expect(JSON.stringify(existingDraft(target))).not.toContain('private fixture');
	expect(JSON.stringify(calls.toast.mock.calls)).not.toContain('private fixture');
	expect(JSON.stringify(calls.toast.mock.calls)).toContain(expected);
	expect(session.text.toString()).toBe(text); expect(onClose).not.toHaveBeenCalled();
});
it('ends the close gate after one flush round trip while another session keeps editing', async () => {
	setup(); edit();
	const onClose = vi.fn(), onBringIntoView = vi.fn();
	const closing = closeTabWithSaveGate(target, { onClose, onBringIntoView });
	const editing = setInterval(() => remote('x'), 1);
	try {
		await vi.waitFor(() => expect(flushes()).toHaveLength(1));
		remote('y');
		// Clean, and the file's digest differs only by remote edits the server owns: never re-saved.
		await complete(await textDigest('server text this tab has not received yet'));
		expect(await within(closing)).toEqual({ closed: true });
	} finally { clearInterval(editing); }
	expect(flushes()).toHaveLength(1);
	expect(onClose).toHaveBeenCalledOnce();
	expect(onBringIntoView).not.toHaveBeenCalled();
});
it('writes nothing for a tab with no local edit: remote text alone rebaselines the draft without a flush', async () => {
	vi.useFakeTimers();
	try {
		setup();
		expect(await flushDraft(target)).toBe(true);
		remote(' from an agent');
		await vi.advanceTimersByTimeAsync(700);
		expect(await flushDraft(target)).toBe(true);
		expect(flushes()).toEqual([]);
		expect(calls.write).not.toHaveBeenCalled();
		expect(existingDraft(target)).toMatchObject({ editText: 'base from an agent', savedText: 'base from an agent', status: 'idle' });
	} finally { vi.useRealTimers(); }
});
it('re-saves while the tab holds an unacknowledged local transaction, and ends once it is clean', async () => {
	setup(); edit();
	const deletion = frames.filter(frame => frame.type === 'doc_update');
	const closing = flushDraft(target);
	await vi.waitFor(() => expect(flushes()).toHaveLength(1));
	session.text.insert(0, 'more'); setDraftText(target, 'more');
	ack(deletion); // the lane received only the deletion before this flush
	session.handle({ type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: flushes()[0].request_id,
		server_saved: true, digest: await textDigest(''), pending: false, held: null });
	await vi.waitFor(() => expect(flushes()).toHaveLength(2));
	await complete();
	expect(await within(closing)).toBe(true);
	expect(flushes()).toHaveLength(2);
	expect(existingDraft(target)).toMatchObject({ status: 'saved' });
});

it('retries an offline local edit through the queue after the live session reopens', async () => {
	vi.useFakeTimers();
	try {
		setup();
		session.disconnected();
		session.text.insert(0, 'offline ');
		setDraftText(target, 'offline base');
		await vi.advanceTimersByTimeAsync(700);

		expect(existingDraft(target)).toMatchObject({ editText: 'offline base', savedText: 'base', status: 'dirty' });
		expect(flushes()).toHaveLength(0);

		session.opened(Y.encodeStateAsUpdate(session.doc), Y.encodeStateVector(session.doc));
		session.handle({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store', pending: false, held: null });
		await vi.waitFor(() => expect(flushes()).toHaveLength(1));
		await complete();

		expect(existingDraft(target)).toMatchObject({ editText: 'offline base', savedText: 'offline base', status: 'saved' });
	} finally { vi.useRealTimers(); }
});

it('does not flush a retained offline draft when its reopen is read-only', async () => {
	vi.useFakeTimers();
	try {
		setup();
		session.disconnected();
		session.text.insert(0, 'offline ');
		setDraftText(target, 'offline base');
		await vi.advanceTimersByTimeAsync(700);

		session.opened(Y.encodeStateAsUpdate(session.doc), Y.encodeStateVector(session.doc), null, false);
		session.handle({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store', pending: false, held: null });

		expect(flushes()).toHaveLength(0);
		expect(existingDraft(target)).toMatchObject({ editText: 'offline base', savedText: 'base', status: 'dirty' });
	} finally { vi.useRealTimers(); }
});
