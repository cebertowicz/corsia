import { fetchLocalModelRecommendations, fetchSources } from '$lib/conductorSources';

export type LocalDownloadRows = {
	active?: boolean;
	speeds?: Map<string, number>;
};

/**
 * Project the one Settings-owned download record onto an existing canonical oMLX
 * server row. This never creates a row: server models remain the only list owner.
 */
export async function localDownloadRows(token: string, serverId: string): Promise<LocalDownloadRows> {
	const [sources, recommendations] = await Promise.allSettled([
		fetchSources(token),
		fetchLocalModelRecommendations(token),
	]);
	const result: LocalDownloadRows = {};
	if (sources.status === 'fulfilled') {
		result.active = sources.value.some((source) => source.source === 'omlx' && source.settings_download);
	}
	if (recommendations.status === 'fulfilled') {
		const downloads = recommendations.value.downloads ?? [];
		const entries = downloads.filter((entry) =>
			entry.status === 'available' && entry.server_id === serverId &&
			typeof entry.speed === 'number' && Number.isFinite(entry.speed),
		);
		result.speeds = new Map(entries.map((entry) => [entry.repository.split('/').at(-1)!, entry.speed!]));
	}
	return result;
}

export function localDownloadMeta(meta: string, speed: number | undefined): string {
	if (speed === undefined) return meta;
	const shown = Number.isInteger(speed) ? String(speed) : String(Number(speed.toFixed(1)));
	return [meta, `${shown} tokens/s`].filter(Boolean).join(' · ');
}
