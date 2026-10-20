import { fetchLocalModelDownloads, fetchSources } from '$lib/conductorSources';

export type LocalDownloadRows = {
	active?: boolean;
	/** The model id CORSIA is loading after its download, else undefined. */
	finishing?: string | null;
	speeds?: Map<string, number>;
	/** Why a downloaded model is not loaded, by model id. */
	notes?: Map<string, string>;
};

/**
 * Project the one Settings-owned download record onto an existing canonical oMLX
 * server row. This never creates a row: server models remain the only list owner.
 */
export async function localDownloadRows(token: string, serverId: string): Promise<LocalDownloadRows> {
	const [sources, downloads] = await Promise.allSettled([
		fetchSources(token),
		fetchLocalModelDownloads(token),
	]);
	const result: LocalDownloadRows = {};
	if (sources.status === 'fulfilled') {
		const flight = sources.value.find((source) => source.source === 'omlx')?.settings_download;
		result.active = Boolean(flight);
		result.finishing = flight?.finishing && flight.repository ? flight.repository.split('/').pop() ?? null : null;
	}
	if (downloads.status === 'fulfilled') {
		const entries = downloads.value.filter((entry) =>
			entry.status === 'available' && entry.server_id === serverId &&
			typeof entry.speed === 'number' && Number.isFinite(entry.speed),
		);
		result.speeds = new Map(entries.map((entry) => [entry.model, entry.speed!]));
		result.notes = new Map(downloads.value
			.filter((entry) => entry.status === 'available' && entry.server_id === serverId && entry.detail)
			.map((entry) => [entry.model, entry.detail!]));
	}
	return result;
}

export function localDownloadMeta(meta: string, speed: number | undefined): string {
	if (speed === undefined) return meta;
	const shown = Number.isInteger(speed) ? String(speed) : String(Number(speed.toFixed(1)));
	return [meta, `${shown} tokens/s`].filter(Boolean).join(' · ');
}

/** The one line under a downloaded model: it is loading now, or why CORSIA did not load it. */
export function localLoadNote(rows: LocalDownloadRows, modelId: string): string | null {
	if (rows.finishing === modelId) return 'Loading on this computer…';
	return rows.notes?.get(modelId) ?? null;
}
