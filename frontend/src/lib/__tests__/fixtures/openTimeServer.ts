import { vi } from 'vitest';

import type { TurnSummary } from '$lib/components/transcriptRunLabel';
import { leadOpenMs } from './focusRuns';

/**
 * Adds the server's open-time answer to a component test's otherwise endpoint-specific fetch stub:
 * the lead thread's own Turns (the ones `summaries` names) clipped to each asked range, nothing else.
 * Like the attention queue, it answers before the stub, so the stub's call counts do not see it.
 */
export function withOpenTime(
	fetchImpl: typeof fetch,
	summaries: () => Record<string, TurnSummary> = () => ({}),
): typeof fetch {
	return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		if (!String(input).includes('/api/open-time')) return init === undefined ? fetchImpl(input) : fetchImpl(input, init);
		const { items } = JSON.parse(String(init?.body)) as {
			items: { id: string; since: string; until: string | null }[];
		};
		const now = Date.now();
		const open = leadOpenMs(summaries(), now);
		const answers = Object.fromEntries(items.map((item) => [item.id, {
			seconds: (open({ id: item.id, since: Date.parse(item.since), until: item.until ? Date.parse(item.until) : null }) ?? 0) / 1000,
			open_now: false,
			as_of: new Date(now).toISOString(),
		}]));
		return { ok: true, status: 200, json: async () => ({ items: answers }) } as Response;
	}) as typeof fetch;
}
