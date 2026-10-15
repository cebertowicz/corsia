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

export type LocalSetupRetry = { ok: boolean; code?: 'consent_required' };

/** Try again: the same action as the first-run card's for that kind. The starter route never writes oMLX settings. */
export async function retryLocalSetup(kind: 'starter' | 'setup', consentExisting = false): Promise<LocalSetupRetry> {
	const t = get(token);
	if (!t) return { ok: false };
	let result: LocalSetupRetry = { ok: true };
	try {
		if (kind === 'starter') {
			result = { ok: (await addStarterModel(t)).ok };
		} else {
			const response = await setupLocalSource(t, consentExisting);
			result = { ok: response.ok, code: response.code };
		}
	} catch { result = { ok: false }; }
	await read();
	return result;
}

/**
 * Read the local row now and on every models_updated, which the backend sends on each download
 * stage and at its end. Returns the stop function; the picker calls it while it is open.
 */
export function watchLocalStatus(): () => void {
	void read();
	return on('models_updated', () => { void read(); });
}
