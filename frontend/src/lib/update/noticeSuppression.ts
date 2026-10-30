import { derived, writable } from 'svelte/store';
import { setupMode } from '$lib/stores/setupMode';

/** Settings owns this while the Updates surface is visible. */
export const updateToastSuppressed = writable(false);
export const noticeSuppressed = derived(
	[updateToastSuppressed, setupMode],
	([$settingsSuppressed, $setupMode]) => $settingsSuppressed || $setupMode === 'flow',
);
