import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { handleMessage } from '../ws/handlers';
import { on } from '../ws/events';
import { LiveDocumentSession } from './liveDocumentSession';
import type { DocumentFrame } from './documentWire';
import captured from './fixtures/failure-owner-frames.json';

function owner() {
	const sent: Record<string, any>[] = [];
	const session = new LiveDocumentSession({ artifactId: captured[0].artifact_id, storeId: captured[0].store_id,
		doc: new Y.Doc(), writable: true, send: frame => { sent.push(frame); return true; } });
	session.opened(Y.encodeStateAsUpdate(new Y.Doc()), Y.encodeStateVector(new Y.Doc()));
	const send = (fields: Record<string, unknown>) => session.handle({ artifact_id: session.artifactId,
		store_id: session.storeId, ...fields } as DocumentFrame);
	return { session, sent, send };
}

function admit(session: LiveDocumentSession, incarnation: string, clears: object[] = []) {
	session.admitFailures({ type: 'doc_opened', live: true, artifact_id: session.artifactId,
		store_id: session.storeId, setup_incarnation: incarnation, failure_clears: clears } as DocumentFrame);
}

describe('versioned live failures', () => {
	it('consumes captured real publisher frames through canonical forwarding and ignores the stale snapshot', () => {
		// Captured from test_publisher_clear_precedes_stale_saved_snapshot's actual owner/WS boundary.
		const { session } = owner();
		const stops = ['doc_error', 'doc_cleared', 'doc_saved'].map(type => on(type, frame => session.handle(frame as DocumentFrame)));
		try {
			handleMessage(captured[0], () => {});
			expect(session.blockingFailures.map(row => row.kind)).toEqual(['publication_failed']);
			handleMessage(captured[1], () => {});
			handleMessage(captured[2], () => {});
			expect(session.blockingFailures).toEqual([]);
		} finally { stops.forEach(stop => stop()); session.destroy(); }
	});

	it('ack and ordinary saves preserve independent blockers; old clears cannot erase newer failures', () => {
		const { session, sent, send } = owner();
		admit(session, 'manager');
		send({ type: 'doc_error', kind: 'publication_failed', blocking: true, failure_generation: 8, message: 'private fixture reason' });
		send({ type: 'doc_error', kind: 'setup_failed', blocking: true, setup_incarnation: 'manager', setup_revision: 2, message: 'private fixture setup' });
		send({ type: 'doc_ack', message_ids: [sent.at(-1)!.message_id] });
		send({ type: 'doc_saved', server_saved: true, blocking: [] });
		send({ type: 'doc_cleared', kind: 'publication_failed', clears_through: 7 });
		expect(session.blockingFailures.map(row => row.kind)).toEqual(['publication_failed', 'setup_failed']);
		expect(JSON.stringify(session.blockingFailures)).not.toContain('private fixture');
		send({ type: 'doc_cleared', kind: 'setup_failed', setup_incarnation: 'old', clears_through: 99 });
		expect(session.blockingFailures).toHaveLength(2);
		send({ type: 'doc_cleared', kind: 'publication_failed', clears_through: 8, store_id: 'wrong' });
		expect(session.blockingFailures).toHaveLength(2);
	});

	it('matched admission reconciles missed clears while preserving newer/other kinds and device text', () => {
		const { session, send } = owner();
		admit(session, 'old');
		session.text.insert(0, 'local retained text');
		const durability = session.durability.snapshot();
		send({ type: 'doc_error', kind: 'publication_failed', blocking: true, failure_generation: 5 });
		send({ type: 'doc_error', kind: 'store_refused', blocking: true, failure_generation: 9, message: 'disk fixture' });
		send({ type: 'doc_error', kind: 'setup_failed', blocking: true, setup_incarnation: 'old', setup_revision: 12 });
		admit(session, 'new', [{ kind: 'publication_failed', clears_through: 5 },
			{ kind: 'store_refused', clears_through: 8 }, { kind: 'setup_failed', setup_incarnation: 'new', clears_through: 0 }]);
		send({ type: 'doc_saved', setup_incarnation: 'intruder', blocking: [{ kind: 'publication_failed', failure_generation: 5 }] });
		send({ type: 'doc_error', kind: 'setup_failed', blocking: true, setup_incarnation: 'old', setup_revision: 99 });
		expect(session.blockingFailures.map(row => row.kind)).toEqual(['store_refused']);
		expect(session.text.toString()).toBe('local retained text');
		expect(session.durability.snapshot()).toEqual(durability);
	});

	it('a stamped stale request refusal rejects the request without resurrecting a cleared row', async () => {
		const { session, sent, send } = owner();
		send({ type: 'doc_cleared', kind: 'publication_failed', clears_through: 3 });
		const saving = session.flush();
		send({ type: 'doc_error', request_id: sent.at(-1)!.request_id, kind: 'publication_failed',
			failure_generation: 3, message: 'private fixture reason', blocking: true });
		await expect(saving).rejects.toThrow("Live updates couldn't be shared for this document.");
		expect(session.failureLines).toEqual([]);
	});

	it('matched request rejection contains only approved publication copy', async () => {
		const { session, sent, send } = owner();
		const saving = session.flush();
		send({ type: 'doc_error', request_id: sent.at(-1)!.request_id, kind: 'publication_failed',
			message: '{"detail":"private fixture reason"}', blocking: true });
		await expect(saving).rejects.toThrow("Live updates couldn't be shared for this document.");
		expect(session.fileError).not.toContain('private fixture');
	});
});
