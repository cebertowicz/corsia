import { fetchSources, type ModelSource } from '$lib/conductorSources';
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

/** The local model's row from the sources service; throws when the read fails, so the caller keeps what it shows. */
export async function readLocalRow(session: string): Promise<ModelSource | undefined> {
	return (await fetchSources(session)).find((source) => source.source === 'omlx');
}

/** The shown sources with only the local row replaced, so a poll never undoes a newer CLI check. */
export function withLocalRow(sources: ModelSource[], row: ModelSource): ModelSource[] {
	return sources.some((source) => source.source === 'omlx') ? sources.map((source) => (source.source === 'omlx' ? row : source)) : [...sources, row];
}
