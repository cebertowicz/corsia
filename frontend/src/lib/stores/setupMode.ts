import { writable } from 'svelte/store';

/** The app's current setup gate. */
export const setupMode = writable<'login' | 'flow'>('login');
