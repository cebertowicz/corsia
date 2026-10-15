import { describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { DocumentDurability } from './documentDurability';

function browser() {
	const doc = new Y.Doc();
	const state = new DocumentDurability('store-one');
	doc.on('update', (_update, origin) => { if (origin !== 'server') state.localTransaction(); });
	return { doc, text: doc.getText('text'), state };
}

describe('exact browser durability', () => {
	it('a reopened copy is not clean until its close-sync message is acknowledged', () => {
		const { state } = browser();
		expect(state.clean).toBe(false);
		state.messageBuilt('reopen-sync');
		state.acknowledge('store-one', ['reopen-sync']);
		expect(state.clean).toBe(true);
	});

	it('a deletion is local-only even when the Yjs state vector does not change', () => {
		const { doc, text, state } = browser();
		doc.transact(() => text.insert(0, 'hello'), 'server');
		state.messageBuilt('initial-sync');
		state.acknowledge('store-one', ['initial-sync']);
		const vector = Y.encodeStateVector(doc);
		text.delete(0, 2);
		expect(Y.encodeStateVector(doc)).toEqual(vector);
		expect(state.clean).toBe(false);
		state.messageBuilt('delete');
		state.acknowledge('store-one', ['delete']);
		expect(state.clean).toBe(true);
		expect(text.toString()).toBe('llo');
	});

	it('a batch acknowledgement excludes edits made after that message was built', () => {
		const { text, state } = browser();
		text.insert(0, 'first');
		state.messageBuilt('first');
		text.insert(5, ' later');
		state.messageBuilt('later');
		state.acknowledge('store-one', ['first']);
		expect(state.clean).toBe(false);
		state.acknowledge('store-one', ['later']);
		expect(state.clean).toBe(true);
	});

	it('only this store and an exact message built by this browser can make it clean', () => {
		const { text, state } = browser();
		text.insert(0, 'mine');
		state.messageBuilt('mine');
		state.acknowledge('other-store', ['mine']);
		state.acknowledge('store-one', ['other-tab']);
		expect(state.clean).toBe(false);
		state.acknowledge('store-one', ['mine']);
		expect(state.clean).toBe(true);
	});

	it('retrying and re-acknowledging a message cannot cover a later transaction', () => {
		const { text, state } = browser();
		text.insert(0, 'first');
		state.messageBuilt('retry');
		text.delete(0, 1);
		state.messageBuilt('retry'); // Re-sending the same bytes preserves the original generation.
		state.acknowledge('store-one', ['retry']);
		expect(state.clean).toBe(false);
		state.messageBuilt('close-sync');
		state.acknowledge('store-one', ['close-sync']);
		expect(state.clean).toBe(true);
		state.acknowledge('store-one', ['retry']);
		expect(state.clean).toBe(true);
	});

	it('a landed flush writes the transactions made before it started, never ones made after', () => {
		const { text, state } = browser();
		expect(state.unwritten).toBe(false);
		text.insert(0, 'first');
		expect(state.unwritten).toBe(true);
		const started = state.snapshot().localGeneration;
		text.insert(5, ' later');
		state.written(started);
		expect(state.unwritten).toBe(true);
		state.written(state.snapshot().localGeneration);
		expect(state.unwritten).toBe(false);
		state.written(started); // A late answer to an older flush cannot un-write a newer one.
		expect(state.unwritten).toBe(false);
	});

	it('an acknowledgement advances the snapshot generation without inventing local edits', () => {
		const { state } = browser();
		const before = state.snapshot();
		state.messageBuilt('sync');
		state.acknowledge('store-one', ['sync']);
		const after = state.snapshot();
		expect(after.localGeneration).toBe(before.localGeneration);
		expect(after.ackGeneration).toBeGreaterThan(before.ackGeneration);
	});
});
