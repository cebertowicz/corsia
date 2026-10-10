import { writable } from 'svelte/store';

export const paneOpen = writable(false);

export const paneDragging = writable(false);

export function isPaneDrawer(): boolean {
	return typeof window !== 'undefined' && window.innerWidth <= 1024;
}
