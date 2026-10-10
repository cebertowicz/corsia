import { describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { DeviceDocument } from './deviceDocument';

const factory = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock('y-indexeddb', () => ({ IndexeddbPersistence: class {
	constructor(name: string, doc: unknown) { return factory.create(name, doc); }
} }));

function persistence() {
	const transactions: (EventTarget & { mode: string; error: Error | null })[] = [];
	const db = new EventTarget() as EventTarget & { transaction: (...args: any[]) => any };
	db.transaction = vi.fn((_stores: string[], mode = 'readonly') => {
		const transaction = Object.assign(new EventTarget(), { mode, error: null });
		transactions.push(transaction);
		return transaction;
	});
	const provider = { _db: Promise.resolve(db), whenSynced: Promise.resolve(), destroy: vi.fn(async () => {}), clearData: vi.fn() };
	factory.create.mockReturnValue(provider);
	return { db, provider, transactions };
}

describe('device copy durability', () => {
	it('claims device storage only after every write transaction completes', async () => {
		const { db, transactions } = persistence();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await copy.ready;
		const first = db.transaction(['updates'], 'readwrite');
		const second = db.transaction(['updates'], 'readwrite');
		expect(copy.saved).toBe(false);
		first.dispatchEvent(new Event('complete'));
		expect(copy.saved).toBe(false);
		second.dispatchEvent(new Event('complete'));
		expect(copy.saved).toBe(true);
		expect(transactions).toHaveLength(2);
	});

	it('keeps a quota or abort failure visible after a later transaction succeeds', async () => {
		const { db } = persistence();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await copy.ready;
		const failed = db.transaction(['updates'], 'readwrite');
		failed.error = new DOMException('quota full', 'QuotaExceededError');
		failed.dispatchEvent(new Event('abort'));
		const next = db.transaction(['updates'], 'readwrite');
		next.dispatchEvent(new Event('complete'));
		expect(copy.error).toContain('quota full');
		expect(copy.saved).toBe(false);
	});

	it('reports an open failure and preserves the text in the tab', async () => {
		const doc = new Y.Doc();
		doc.getText('text').insert(0, 'only copy');
		factory.create.mockReturnValue({ _db: Promise.reject(new Error('storage unavailable')), whenSynced: new Promise(() => {}) });
		const copy = new DeviceDocument('artifact', 'store', doc);
		await copy.ready;
		expect(copy.saved).toBe(false);
		expect(copy.error).toBe('storage unavailable');
		expect(doc.getText('text').toString()).toBe('only copy');
	});

	it('uses artifact and store identity, and destroy never collects another tab copy', async () => {
		const { provider } = persistence();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await copy.ready;
		expect(factory.create.mock.calls.at(-1)?.[0]).toBe('corsia-document:artifact:store');
		await copy.destroy();
		expect(provider.destroy).toHaveBeenCalledOnce();
		expect(provider.clearData).not.toHaveBeenCalled();
	});
});
