/** Attention API client types and parent roll-up request. */

export interface AttentionItem {
	external_id: string;
	/** Human-readable artifact name (from artifact_identities.title). */
	name?: string | null;
	integration: string;
	type: string;
	subtype?: string | null;
	attention_state?: string | null;
	requested_action?: string | null;
	urgency?: 'high' | 'medium' | 'low' | null;
	confidence?: string | null;
	disposition_outcome?: string | null;
	defer_condition?: { type: string; value?: string } | null;
	owner_thread?: string | null;
	levels?: string[];
	hold_ids?: string[];
}

export interface RollupResponse {
	parent_thread_id: string;
	items: AttentionItem[];
	pull_only: boolean;
}

async function req<T>(url: string, options?: RequestInit): Promise<T> {
	const res = await fetch(url, options);
	if (!res.ok) {
		const text = await res.text().catch(() => '');
		const err: Error & { status?: number; body?: string } = new Error(
			`HTTP ${res.status}: ${text}`
		);
		err.status = res.status;
		err.body = text;
		throw err;
	}
	return res.json() as Promise<T>;
}

function qs(params: Record<string, string | undefined | null>): string {
	const p = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v != null) p.set(k, v);
	}
	const s = p.toString();
	return s ? `?${s}` : '';
}

/** GETs carry the token as a Bearer header (not in the URL) — backend accepts it
 * via the shared require_user seam. */
function bearer(token: string): RequestInit {
	return { headers: { Authorization: `Bearer ${token}` } };
}

export async function fetchRollup(token: string, parentThreadId: string): Promise<RollupResponse> {
	return req<RollupResponse>(
		`/api/attention/rollup${qs({ parent_thread_id: parentThreadId })}`, bearer(token)
	);
}
