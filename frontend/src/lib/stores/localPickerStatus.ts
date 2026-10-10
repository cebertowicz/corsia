import { derived, get, writable } from 'svelte/store';
import { fetchSources, localPickerStatus, setupLocalSource, type ModelSource } from '$lib/conductorSources';
import { addStarterModel } from '$lib/conductorSourcesApi';
import { on } from '$lib/ws/events';
import { token } from './auth';

/** The local source row, as the model picker last read it. */
const localSource = writable<ModelSource | undefined>(undefined);

/** What the picker's Local group says about a download or a failed setup; null when neither. */
export const localStatus = derived(localSource, localPickerStatus);

async function read(): Promise<void> {
	const t = get(token);
	if (!t) return;
	try {
		localSource.set((await fetchSources(t)).find((source) => source.source === 'omlx'));
	} catch { /* the line stays as it was */ }
}

/** Try again: the same action as the first-run card's for that kind. The starter route never writes oMLX settings. */
export async function retryLocalSetup(kind: 'starter' | 'setup'): Promise<boolean> {
	const t = get(token);
	if (!t) return false;
	let ok = true;
	try {
		const result = kind === 'starter' ? await addStarterModel(t) : await setupLocalSource(t);
		ok = result.ok;
	} catch { ok = false; }
	await read();
	return ok;
}

/**
 * Read the local row now and on every models_updated, which the backend sends on each download
 * stage and at its end. Returns the stop function; the picker calls it while it is open.
 */
export function watchLocalStatus(): () => void {
	void read();
	return on('models_updated', () => { void read(); });
}
