import { fetchSources, type ModelSource } from '$lib/conductorSources';
import { setupCatalog, type SetupModel } from '$lib/setupModels';
import type { LocalRowState } from '$lib/setupProviderRows';

/** The local download before an account exists, read from the setup service; null when it can't be read, so the last reading stays. */
export async function readPreauthLocal(): Promise<LocalRowState | null> {
	try {
		const response = await fetch('/api/setup/local-download');
		if (!response.ok) return null;
		const live = await response.json();
		return { source: 'omlx', name: live.model ?? 'Local model', state: live.state, local_state: live.state,
			download_percent: live.percent, download_eta_seconds: live.eta_seconds, download_size: live.download_size, download_stalled: live.stalled,
			setup_error_code: live.cause, setup_error_reference: live.ref, bytes_to_free: live.bytes_to_free, fix: live.fix, detail: live.cause === 'load_refused' ? live.catalog_line ?? undefined : undefined };
	} catch { return null; }
}

/**
 * One signed-in poll of the local model: its row replaces the shown one and nothing else, so the poll never undoes a newer CLI check.
 * The catalog, which says whether the loaded model works, is read only when the row's state moved, not on a percent.
 * Throws when a read fails, so the caller keeps what it shows.
 */
export async function readLocalSource(session: string, sources: ModelSource[], catalog: SetupModel[]): Promise<{ sources: ModelSource[]; catalog: SetupModel[] }> {
	const row = (await fetchSources(session)).find((source) => source.source === 'omlx');
	if (!row) return { sources, catalog };
	const shown = sources.find((source) => source.source === 'omlx');
	const next = row.local_state !== shown?.local_state ? await setupCatalog(session) : catalog;
	return { sources: shown ? sources.map((source) => (source === shown ? row : source)) : [...sources, row], catalog: next };
}
