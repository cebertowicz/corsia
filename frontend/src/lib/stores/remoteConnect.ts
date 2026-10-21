import { writable } from 'svelte/store';

/**
 * Settings › System › Account's "Connect to another computer" asks the home view to open setup's Connect screens
 * (r1) over the signed-in app. The desktop shell navigates the window away once a pairing succeeds.
 */
export const connectOpen = writable(false);
