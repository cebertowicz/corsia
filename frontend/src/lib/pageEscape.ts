import { get } from 'svelte/store';
import { paneOpen } from '$lib/stores/paneOpen';
import { phoneNavOver } from '$lib/stores/phoneNav.svelte';

/**
 * The home page's own Escape, run from its window listener. Escape closes the topmost layer: a layer above the page (a
 * dialog, the palette, a sheet) takes it first and prevents the default, so the page leaves it alone; then the phone
 * navigation, which slides over the pane and any sheet in it; then the pane; then the sidebar. Returns whether it
 * closed something.
 */
export function pageEscape(event: KeyboardEvent, sidebar: { open: boolean; close: () => void }): boolean {
	if (event.key !== 'Escape' || event.defaultPrevented) return false;
	if (get(phoneNavOver)) { sidebar.close(); return true; }
	if (get(paneOpen)) { paneOpen.set(false); return true; }
	if (sidebar.open) { sidebar.close(); return true; }
	return false;
}
