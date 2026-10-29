import { derived } from 'svelte/store';
import { threads, coordinationThreads } from './threads';

/** Every listed thread, coordination rows included, for readers that search all rows or look one up by id. */
export const everyThread = derived([threads, coordinationThreads], ([$threads, $coordination]) => [...$threads, ...$coordination]);
