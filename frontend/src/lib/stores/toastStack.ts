import { writable } from 'svelte/store';

/** Height in px the visible toast stack covers, 0 when none shows. A scrolling surface pads its end by it. */
export const toastStackHeight = writable(0);
