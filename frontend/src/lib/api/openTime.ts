/** One batched read of open time, the server's number for how long running work has had something happening in it. */

export interface OpenTimeQuery {
	/** Names the answer; unique within one request. */
	key: string;
	/** The lead thread; its descendants are included by the server. */
	threadId: string;
	/** Epoch ms. */
	since: number;
	/** Epoch ms, or null for a range that runs to now. */
	until: number | null;
}

export interface OpenTimeAnswer {
	seconds: number;
	open_now: boolean;
	as_of: string;
}

/** The answers by query key; a thread the user cannot read has none. Null when the read failed. */
export async function readOpenTime(
	bearerValue: string,
	queries: OpenTimeQuery[],
): Promise<Record<string, OpenTimeAnswer> | null> {
	try {
		const res = await fetch('/api/open-time', {
			method: 'POST',
			headers: { Authorization: `Bearer ${bearerValue}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({
				items: queries.map((query) => ({
					id: query.key,
					thread_id: query.threadId,
					since: new Date(query.since).toISOString(),
					until: query.until === null ? null : new Date(query.until).toISOString(),
				})),
			}),
		});
		if (!res.ok) return null;
		const body = (await res.json()) as { items?: Record<string, OpenTimeAnswer> } | null;
		return body?.items ?? null;
	} catch {
		return null;
	}
}
