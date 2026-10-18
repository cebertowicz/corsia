import { derived, get, writable } from 'svelte/store';
import { fetchSources, localPickerStatus, setupLocalSource, startLocalModelDownload, type LocalPickerRetry, type ModelSource } from '$lib/conductorSources';
import { addStarterModel } from '$lib/conductorSourcesApi';
import { on } from '$lib/ws/events';
import { token } from './auth';

/** The local source row, as the model picker last read it. */
const localSource = writable<ModelSource | undefined>(undefined);

/** What the picker's Local group says about a download or a failed setup; null when neither. */
export const localStatus = derived(localSource, localPickerStatus);
/** The picker may offer the shared add-model sheet only on Macs that pass the API memory gate. */
export const canAddLocalModel = derived(localSource, (source) => source?.eligible === true);

async function read(): Promise<void> {
	const t = get(token);
	if (!t) return;
	try {
		localSource.set((await fetchSources(t)).find((source) => source.source === 'omlx'));
	} catch { /* the line stays as it was */ }
}

/** ``message`` is the backend's specific refusal when it gave one. */
export type LocalSetupRetry = { ok: boolean; code?: 'consent_required'; message?: string };

/** Try again: preserve each download's owner route; Settings retries its durable repository. */
export async function retryLocalSetup(kind: LocalPickerRetry, consentExisting = false): Promise<LocalSetupRetry> {
	const t = get(token);
	if (!t) return { ok: false };
	let result: LocalSetupRetry = { ok: true };
	try {
		if (kind === 'starter') {
			result = { ok: (await addStarterModel(t)).ok };
		} else if (kind === 'settings') {
			const repository = get(localSource)?.settings_download?.repository;
			if (!repository) result = { ok: false };
			else {
				await startLocalModelDownload(t, repository);
				result = { ok: true };
			}
		} else {
			const response = await setupLocalSource(t, consentExisting);
			result = { ok: response.ok, code: response.code };
		}
	} catch (caught) { result = { ok: false, message: caught instanceof Error ? caught.message : undefined }; }
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
