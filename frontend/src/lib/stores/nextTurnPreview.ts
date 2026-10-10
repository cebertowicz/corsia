import type { ContextUsage } from './contextUsageTypes';

/**
 * Read the next-turn context preview for one thread.
 *
 * The one request behind both the context sheet and the composer chip after a
 * fold (#1562), so the two surfaces cannot drift in URL, model selection or
 * error handling. A non-OK response throws; callers decide how to show it.
 */
export async function fetchNextTurnPreview(
	threadId: string,
	authToken: string,
	modelId: string | null,
): Promise<ContextUsage | null> {
	const params = new URLSearchParams();
	params.set('token', authToken);
	if (modelId) params.set('model', modelId);
	const res = await fetch(
		`/api/threads/${threadId}/context-snapshot/next-turn?${params.toString()}`,
	);
	if (!res.ok) throw new Error(`Context preview failed (${res.status})`);
	const body = await res.json();
	return body?.preview ?? null;
}
