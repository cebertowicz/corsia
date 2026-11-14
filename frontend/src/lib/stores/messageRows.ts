import { writable } from 'svelte/store';
import { pruneReplacedRows } from '../messageReplacements';

/** Every transcript writer observes the same monotonic removal facts. */
export function messageRows<T extends { id: string; thread_id: string }>() {
	const store = writable<T[]>([]);
	return {
		subscribe: store.subscribe,
		set: (rows: T[]) => store.set(pruneReplacedRows(rows)),
		update: (change: (rows: T[]) => T[]) => store.update(rows => pruneReplacedRows(change(rows))),
	};
}
