import { writable } from 'svelte/store';

export const focusedDocComment = writable<string | null>(null);
