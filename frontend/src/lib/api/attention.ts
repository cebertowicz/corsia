/**
 * Attention API client (coordination MVP S6b).
 *
 * Thin wrappers over the /api/attention/* endpoints. No local caching or
 * badge/unread state — the surface is pull-only and the caller refreshes.
 *
 * There is one attention pile (PLANS-AC29). `/raised` and `/rollup` return the
 * rows no typed renderer can draw; the rows the attention dock renders are
 * omitted by the server, which proves it can draw them before removing them.
 * Nothing is filtered here.
 */

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

export interface AttentionList {
	items: AttentionItem[];
}

export interface RollupResponse {
	parent_thread_id: string;
	items: AttentionItem[];
	pull_only: boolean;
}

export interface ReviewResult {
	artifact_id: string;
	profile_id?: string;
	/** True when a blocking finding exists and may_approve_to_user is false. */
	blocking_failure: boolean;
	/** False when approve_to_user is gated; use this to disable the Approve button. */
	may_approve_to_user: boolean;
	/** Raw parent-disposition recommendation enum — do NOT render directly. */
	recommendation?: string;
	findings: Array<{ severity: string; message: string }>;
}

export interface ClearResult {
	artifact_id: string;
	outcome: string;
	attention_state: string;
	cleared_levels: string[];
	artifact_archived: boolean;
	subscription_archived: boolean;
	defer_condition?: Record<string, unknown> | null;
}

export interface DisposeResult {
	artifact_id: string;
	outcome: string;
	user_visible: boolean;
	attention_state: string;
	routed_to?: string | null;
	route_disposition?: string | null;
	reason?: string | null;
	audit?: Record<string, unknown>;
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

export async function fetchRaised(token: string, threadId?: string | null): Promise<AttentionList> {
	return req<AttentionList>(
		`/api/attention/raised${qs({ thread_id: threadId ?? null })}`, bearer(token)
	);
}

export async function fetchRollup(token: string, parentThreadId: string): Promise<RollupResponse> {
	return req<RollupResponse>(
		`/api/attention/rollup${qs({ parent_thread_id: parentThreadId })}`, bearer(token)
	);
}

export async function fetchReview(
	token: string,
	artifactId: string,
	profileId?: string | null
): Promise<ReviewResult> {
	return req<ReviewResult>(
		`/api/attention/${encodeURIComponent(artifactId)}/review${qs({ profile_id: profileId ?? null })}`,
		bearer(token)
	);
}

export async function clearAttention(
	token: string,
	artifactId: string,
	outcome: string,
	opts?: { reason?: string; condition?: Record<string, unknown>; hold_ids?: string[] }
): Promise<ClearResult> {
	return req<ClearResult>(`/api/attention/${encodeURIComponent(artifactId)}/clear`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ token, outcome, reason: opts?.reason, condition: opts?.condition, hold_ids: opts?.hold_ids })
	});
}

export async function disposeAttention(
	token: string,
	artifactId: string,
	outcome: string,
	opts?: {
		reason?: string;
		answer?: string;
		authority?: string;
		override_blocking?: boolean;
		new_owner?: string;
		condition?: Record<string, unknown>;
		replacement_id?: string;
		actor_thread?: string;
		hold_ids?: string[];
	}
): Promise<DisposeResult> {
	return req<DisposeResult>(`/api/attention/${encodeURIComponent(artifactId)}/dispose`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ token, outcome, ...opts })
	});
}
