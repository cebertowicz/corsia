import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { LiveDocumentSession } from './liveDocumentSession';
import { currentLiveUpdateAuthor, liveUpdateAuthor } from './liveUpdateAuthor';

function open() {
	const server = new Y.Doc();
	server.getText('text').insert(0, 'hello');
	const session = new LiveDocumentSession({
		artifactId: 'artifact', storeId: 'store', doc: new Y.Doc(), writable: true, send: () => true,
	});
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	return { server, session };
}

const frame = (server: Y.Doc, author?: unknown) => ({
	type: 'doc_update', artifact_id: 'artifact', store_id: 'store', author,
	update: btoa(String.fromCharCode(...Y.encodeStateAsUpdate(server))),
});

describe('live update author', () => {
	it('row 0: a write names the streamed call it commits, when it has one', () => {
		expect(liveUpdateAuthor({ thread_id: 't1', name: 'Alpha', call_id: 'toolu_1' })).toEqual({ threadId: 't1', name: 'Alpha', callId: 'toolu_1' });
		expect(liveUpdateAuthor({ thread_id: 't1', name: 'Alpha', call_id: 7 })).toEqual({ threadId: 't1', name: 'Alpha' });
	});

	it('row 1: the writer is known while the broadcast update is applied, and cleared after', () => {
		const { server, session } = open();
		const seen: unknown[] = [];
		session.text.observe(() => seen.push(currentLiveUpdateAuthor()));
		server.getText('text').insert(5, ' there');
		session.handle(frame(server, { thread_id: 't1', name: 'Alpha' }) as never);
		expect(seen).toEqual([{ threadId: 't1', name: 'Alpha' }]);
		expect(currentLiveUpdateAuthor()).toBeNull();
	});

	it('row 24: an update without an author reports none', () => {
		const { server, session } = open();
		const seen: unknown[] = [];
		session.text.observe(() => seen.push(currentLiveUpdateAuthor()));
		server.getText('text').insert(0, 'x');
		session.handle(frame(server) as never);
		expect(seen).toEqual([null]);
	});

	it('treats a malformed author as none', () => {
		expect(liveUpdateAuthor({ thread_id: 5, name: 'x' })).toBeNull();
		expect(liveUpdateAuthor({ thread_id: 'a', name: '' })).toBeNull();
		expect(liveUpdateAuthor('Alpha')).toBeNull();
	});
});
