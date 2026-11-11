import type { ContextUsage } from './contextUsageTypes';
import { apiFetch } from '$lib/api/request';

/**
 * Read the next-turn context preview for one thread.
 *
 * The one request behind both the context sheet and the composer chip after a
 * fold (#1562), so the two surfaces cannot drift in URL, model selection or
 * error handling. A non-OK response throws; callers decide how to show it.
 */
export async function fetchNextTurnPreview(
	threadId: string,
	modelId: string | null,
): Promise<ContextUsage | null> {
	const query = modelId ? `?model=${encodeURIComponent(modelId)}` : '';
	const res = await apiFetch(
		`/api/threads/${threadId}/context-snapshot/next-turn${query}`,
		{ sessionInAddress: true },
	);
	if (!res.ok) throw new Error(`Context preview failed (${res.status})`);
	const body = await res.json();
	return body?.preview ?? null;
}
