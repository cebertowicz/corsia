import * as Y from 'yjs';
import { IndexeddbPersistence } from 'y-indexeddb';
import { writable } from 'svelte/store';

/** A retained copy; server acknowledgements never collect it. */
export class DeviceDocument {
	readonly persistence: IndexeddbPersistence;
	readonly ready: Promise<void>;
	saved = false;
	error = '';
	private pending = 0;
	private closed = false;
	private releaseFailure: () => void = () => {};
	private originalTransaction: IDBDatabase['transaction'] = () => { throw new Error('Device storage is not open'); };
	private state = writable<DeviceDocument>(this);
	readonly subscribe = this.state.subscribe;

	constructor(artifactId: string, storeId: string, doc: Y.Doc) {
		this.persistence = new IndexeddbPersistence(`corsia-document:${encodeURIComponent(artifactId)}:${encodeURIComponent(storeId)}`, doc);
		const failure = new Promise<void>(resolve => { this.releaseFailure = resolve; });
		const loaded = this.persistence._db.then(async db => {
			this.observe(db);
			await this.persistence.whenSynced;
			await this.synced();
		}).catch(error => { this.failed(error); });
		this.ready = Promise.race([loaded, failure]);
	}

	private changed(): void { if (!this.closed) this.state.set(this); }

	private failed(error: unknown): void {
		this.error = error instanceof Error || error instanceof DOMException ? error.message : 'Device storage failed';
		this.saved = false;
		this.releaseFailure();
		this.changed();
	}

	private async synced(): Promise<void> {
		// The library's fetch transaction is readwrite and unobservable (it began before
		// observe()) and `synced` fires before it commits. A readonly transaction on the
		// original connection cannot start until every earlier overlapping writer ends.
		await this.barrier();
		if (this.closed || this.error || this.pending > 0) return;
		this.saved = true;
		this.changed();
	}

	private barrier(): Promise<void> {
		return new Promise((resolve, reject) => {
			const tx = this.originalTransaction(['updates'], 'readonly');
			tx.addEventListener('complete', () => resolve(), { once: true });
			tx.addEventListener('abort', () => reject(tx.error ?? new Error('Device storage check was aborted')), { once: true });
		});
	}

	private observe(db: IDBDatabase): void {
		// Observe this provider's connection only. The library remains the sole
		// update/compaction writer; a request's success is not a committed copy.
		const transaction = db.transaction.bind(db);
		this.originalTransaction = transaction;
		db.addEventListener('error', event => {
			this.failed((event.target as IDBRequest | null)?.error ?? new Error('Device storage failed'));
		});
		db.addEventListener('close', () => this.failed(new Error('Device storage closed')));
		db.transaction = ((...args: Parameters<IDBDatabase['transaction']>) => {
			let tx: IDBTransaction;
			try { tx = transaction(...args); }
			catch (error) { this.failed(error); throw error; }
			if (tx.mode === 'readonly') return tx;
			this.pending++;
			this.saved = false;
			this.changed();
			tx.addEventListener('complete', () => {
				this.pending--;
				this.saved = this.pending === 0 && !this.error;
				this.changed();
			}, { once: true });
			tx.addEventListener('abort', () => {
				this.pending--;
				this.failed(tx.error ?? new Error('Device write was aborted'));
			}, { once: true });
			return tx;
		}) as IDBDatabase['transaction'];
	}

	async destroy(): Promise<void> {
		this.closed = true;
		try { await this.persistence.destroy(); }
		catch (error) { this.failed(error); }
	}
}
