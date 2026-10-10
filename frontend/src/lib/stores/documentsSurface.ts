import { writable } from 'svelte/store';

export type DocLens = 'all' | 'skills' | 'recent' | 'activity';

export const docLens = writable<DocLens>('all');
export const selectedDocId = writable<string | null>(null);
