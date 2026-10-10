import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { LiveDocumentSession } from './liveDocumentSession';

function pair() {
	const server = new Y.Doc();
	server.getText('text').insert(0, 'hello');
	const sent: Record<string, any>[] = [];
	const session = new LiveDocumentSession({
		artifactId: 'artifact', storeId: 'store', doc: new Y.Doc(), writable: true,
		send: (frame) => { sent.push(frame); return true; },
	});
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	const apply = (frame: Record<string, any>) => Y.applyUpdate(server, Uint8Array.from(atob(frame.update), c => c.charCodeAt(0)));
	const ack = (frame: Record<string, any>) => session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: [frame.message_id] });
	return { server, session, sent, apply, ack };
}

describe('live browser session', () => {
	it('uses only the pending field across open, refused promotion and settlement frames', async () => {
		const { session, sent } = pair();
		session.handle({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store', pending: true, held: null });
		expect(session.pending).toBe(true);
		session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: [sent.at(-1)!.message_id] });
		expect(session.pending).toBe(true);
		session.handle({ type: 'doc_pending', artifact_id: 'artifact', store_id: 'other', pending: false, held: null });
		expect(session.pending).toBe(true);
		session.handle({ type: 'doc_pending', artifact_id: 'artifact', store_id: 'store', pending: false, held: null });
		expect(session.pending).toBe(false);
		const flushing = session.flush();
		const request = sent.at(-1)!;
		session.handle({ type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id,
			server_saved: false, pending: true, held: null });
		await expect(flushing).rejects.toThrow('Written, not stored');
		expect(session.pending).toBe(true);
		session.handle({ type: 'doc_pending', artifact_id: 'artifact', store_id: 'store', pending: false,
			held: { corsia_text: 'live', file_text: 'outside', candidate_text: 'candidate', choice_pending: false } });
		expect(session.pending).toBe(false);
		expect(session.held?.file_text).toBe('outside');
		expect(session.text.toString()).toBe('hello');
	});

	it('explicit recovery Retry uses the existing lane retry frame', async () => {
		const { session, sent } = pair();
		const retrying = session.retry();
		const request = sent.at(-1)!;
		expect(request).toMatchObject({ type: 'doc_flush', retry: true });
		session.handle({ type: 'doc_saved', artifact_id: 'artifact', store_id: 'store',
			request_id: request.request_id, server_saved: true, pending: false, held: null, blocking: [] });
		await retrying;
	});

	it('merges local and remote inserts without a whole-text replacement', () => {
		const { server, session, sent, apply } = pair();
		session.text.insert(5, ' local');
		server.getText('text').insert(0, 'remote ');
		session.handle({ type: 'doc_update', artifact_id: 'artifact', store_id: 'store', update: btoa(String.fromCharCode(...Y.encodeStateAsUpdate(server))) });
		apply(sent.at(-1)!);
		expect(session.text.toString()).toBe(server.getText('text').toString());
		expect(session.text.toString()).toContain('local');
		expect(session.text.toString()).toContain('remote');
	});

	it('sends the complete delete set on reconnect even with equal state vectors', () => {
		const { server, session, sent, apply, ack } = pair();
		ack(sent.at(-1)!);
		session.disconnected();
		session.text.delete(0, 2);
		expect(Y.encodeStateVector(session.doc)).toEqual(Y.encodeStateVector(server));
		expect(session.durability.clean).toBe(false);
		session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
		const sync = sent.at(-1)!;
		expect(sync.type).toBe('doc_sync');
		apply(sync);
		ack(sync);
		expect(server.getText('text').toString()).toBe('llo');
		expect(session.durability.clean).toBe(true);
	});

	it('keeps store-refused text and retries the identical bytes and message id', () => {
		const { session, sent, ack } = pair();
		ack(sent.at(-1)!);
		session.text.delete(0, 1);
		const edit = sent.at(-1)!;
		session.handle({ type: 'doc_error', artifact_id: 'artifact', kind: 'store_refused', message: 'disk full' });
		expect(session.storeError).toBe('disk full');
		expect(session.durability.clean).toBe(false);
		session.retryStore();
		expect(sent.at(-1)).toEqual(edit);
		ack(edit);
		expect(session.storeError).toBe('');
		expect(session.text.toString()).toBe('ello');
	});

	it('a receipt does not make a deletion durable and an old-store frame cannot alter text', () => {
		const { session, sent, ack } = pair();
		ack(sent.at(-1)!);
		session.text.delete(0, 1);
		const deletion = sent.at(-1)!;
		session.handle({ type: 'doc_received', artifact_id: 'artifact', store_id: 'store', message_ids: [deletion.message_id] });
		expect(session.durability.clean).toBe(false);
		const other = new Y.Doc();
		other.getText('text').insert(0, 'wrong store');
		session.handle({ type: 'doc_update', artifact_id: 'artifact', store_id: 'old', update: btoa(String.fromCharCode(...Y.encodeStateAsUpdate(other))) });
		expect(session.text.toString()).toBe('ello');
	});

	it('close sends a full sync before closing the server subscription', () => {
		const { session, sent } = pair();
		session.text.delete(0, 1);
		session.close();
		expect(sent.slice(-2).map(frame => frame.type)).toEqual(['doc_sync', 'doc_close']);
	});

	it('applies transaction deltas, including deletions, without echoing them as local edits', () => {
		const { server, session, sent } = pair();
		const before = Y.encodeStateVector(server);
		server.transact(() => {
			server.getText('text').delete(0, 2);
			server.getText('text').insert(0, '🌍 ');
		});
		const sentBefore = sent.length;
		const generation = session.durability.snapshot().localGeneration;
		session.handle({ type: 'doc_update', artifact_id: 'artifact', store_id: 'store',
			update: btoa(String.fromCharCode(...Y.encodeStateAsUpdate(server, before))) });
		expect(session.text.toString()).toBe('🌍 llo');
		expect(session.text.toString()).toBe(server.getText('text').toString());
		expect(sent).toHaveLength(sentBefore);
		expect(session.durability.snapshot().localGeneration).toBe(generation);
	});

	it('read-only remote sync emits no update or user flush', async () => {
		const sent: Record<string, any>[] = [];
		const session = new LiveDocumentSession({ artifactId: 'artifact', storeId: 'store', doc: new Y.Doc(), writable: false,
			send: frame => { sent.push(frame); return true; } });
		const server = new Y.Doc();
		server.getText('text').insert(0, 'read only');
		session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
		await expect(session.flush()).rejects.toThrow('read-only');
		expect(sent).toEqual([]);
		expect(session.text.toString()).toBe('read only');
	});
});
