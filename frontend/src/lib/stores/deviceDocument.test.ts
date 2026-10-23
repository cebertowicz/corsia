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

type FakeTransaction = EventTarget & { mode: string; error: Error | null };
type FakeDb = EventTarget & { transaction: (...args: any[]) => FakeTransaction };

/** Behaves like y-indexeddb: stored updates replay with the provider as origin, and only other updates write. */
function fakeIndexeddb(stored: Map<string, Uint8Array[]>, transactions: FakeTransaction[]) {
	return (name: string, doc: Y.Doc) => {
		const db = new EventTarget() as FakeDb;
		db.transaction = (_stores: string[], mode = 'readonly') => {
			const transaction = Object.assign(new EventTarget(), { mode, error: null });
			transactions.push(transaction);
			return transaction;
		};
		const provider: Record<string, unknown> = { destroy: vi.fn(async () => {}), clearData: vi.fn() };
		provider._db = Promise.resolve(db);
		provider.whenSynced = (provider._db as Promise<FakeDb>).then(() => {
			// The library's own fetch runs before the DeviceDocument can observe the connection.
			for (const update of stored.get(name) ?? []) Y.applyUpdate(doc, update, provider);
		});
		doc.on('update', (update: Uint8Array, origin: unknown) => {
			if (origin === provider) return;
			db.transaction(['updates'], 'readwrite').addEventListener('complete', () => {
				stored.set(name, [...(stored.get(name) ?? []), update]);
			});
		});
		return provider;
	};
}

const settle = () => new Promise<void>(resolve => setTimeout(resolve));

function deferredSync() {
	let release: () => void = () => {};
	let reject: (error: Error) => void = () => {};
	const whenSynced = new Promise<void>((resolve, fail) => { release = resolve; reject = fail; });
	const { db, provider, transactions } = persistence();
	provider.whenSynced = whenSynced;
	return { db, provider, transactions, release, reject };
}

describe('device copy saved after sync', () => {
	it('is saved on reopen with unchanged content and no further write', async () => {
		const stored = new Map<string, Uint8Array[]>();
		const transactions: FakeTransaction[] = [];
		factory.create.mockImplementation(fakeIndexeddb(stored, transactions));
		const firstDoc = new Y.Doc();
		const first = new DeviceDocument('artifact', 'store', firstDoc);
		await first.ready;
		firstDoc.getText('text').insert(0, 'kept text');
		expect(first.saved).toBe(false);
		transactions.at(-1)?.dispatchEvent(new Event('complete'));
		expect(first.saved).toBe(true);
		await first.destroy();

		const writes = transactions.length;
		const secondDoc = new Y.Doc();
		const second = new DeviceDocument('artifact', 'store', secondDoc);
		await second.ready;
		expect(secondDoc.getText('text').toString()).toBe('kept text');
		expect(transactions).toHaveLength(writes);
		expect(second.saved).toBe(true);
		expect(second.error).toBe('');
	});

	it('is saved on a fresh store once the first write completes', async () => {
		const transactions: FakeTransaction[] = [];
		factory.create.mockImplementation(fakeIndexeddb(new Map(), transactions));
		const doc = new Y.Doc();
		const copy = new DeviceDocument('artifact', 'fresh', doc);
		await copy.ready;
		doc.getText('text').insert(0, 'server text');
		expect(copy.saved).toBe(false);
		transactions.at(-1)?.dispatchEvent(new Event('complete'));
		expect(copy.saved).toBe(true);
	});

	it('stays unsaved while a write began before the sync resolved, until it completes', async () => {
		const { db, release, transactions } = deferredSync();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await settle();
		db.transaction(['updates'], 'readwrite');
		release();
		await copy.ready;
		expect(copy.saved).toBe(false);
		transactions.at(-1)?.dispatchEvent(new Event('complete'));
		expect(copy.saved).toBe(true);
	});

	it('goes unsaved when a write begins after the sync and saved again when it completes', async () => {
		const { db, transactions } = persistence();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await copy.ready;
		expect(copy.saved).toBe(true);
		db.transaction(['updates'], 'readwrite');
		expect(copy.saved).toBe(false);
		transactions.at(-1)?.dispatchEvent(new Event('complete'));
		expect(copy.saved).toBe(true);
	});

	it('stays unsaved when a device error precedes a later sync', async () => {
		const { db, release } = deferredSync();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await settle();
		const failed = db.transaction(['updates'], 'readwrite');
		failed.error = new DOMException('quota full', 'QuotaExceededError');
		failed.dispatchEvent(new Event('abort'));
		await copy.ready;
		release();
		await settle();
		expect(copy.error).toContain('quota full');
		expect(copy.saved).toBe(false);
	});

	it('stays unsaved when the database closed before the sync', async () => {
		const { db, release } = deferredSync();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await settle();
		db.dispatchEvent(new Event('close'));
		release();
		await copy.ready;
		await settle();
		expect(copy.error).toBe('Device storage closed');
		expect(copy.saved).toBe(false);
	});

	it('stays unsaved when the sync rejects', async () => {
		const { reject } = deferredSync();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await settle();
		reject(new Error('sync failed'));
		await copy.ready;
		expect(copy.error).toBe('sync failed');
		expect(copy.saved).toBe(false);
	});

	it('stays unsaved when the copy is destroyed before the sync resolves', async () => {
		const { release } = deferredSync();
		const copy = new DeviceDocument('artifact', 'store', new Y.Doc());
		await settle();
		await copy.destroy();
		release();
		await copy.ready;
		expect(copy.saved).toBe(false);
	});
});

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
