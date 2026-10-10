import { writable } from 'svelte/store';

export type ThreadState =
	| 'new'
	| 'planning'
	| 'proposed'
	| 'inflight'
	| 'question'
	| 'paused'
	| 'failed'
	| 'done';

export interface OpenThread {
	name: string;
	state?: ThreadState;
}

export const openThread = writable<OpenThread | null>(null);
