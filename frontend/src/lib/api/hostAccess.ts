export type HostAccessLeaseState =
	| 'reviewing'
	| 'awaiting_user'
	| 'activating'
	| 'active'
	| 'expired'
	| 'revoked'
	| 'blocked'
	| 'rejected';

export interface HostAccessReviewer {
	model: string;
	display_name: string | null;
	attestation: string | null;
}

export interface HostAccessApplicationReview {
	allowed?: boolean;
	reason?: string | null;
	attestation?: string | null;
	reviewer_display_name?: string | null;
	approval_mode?: 'manual' | 'automatic';
	[key: string]: unknown;
}

export interface HostAccessRepoPolicy {
	repo_name: string;
	repo_path: string;
	pinned_branch: string | null;
	protect_base_checkout: boolean;
}

export type HostAccessPolicyExceptionKind =
	| 'branch_pinning'
	| 'protected_base_checkout'
	| 'external_agent';

export interface HostAccessRepoPolicyException {
	repo_name: string;
	policy: 'branch_pinning' | 'protected_base_checkout';
	reason: string;
}

export interface HostAccessExternalAgentException {
	policy: 'external_agent';
	reason: string;
	command: string;
	working_directory: string;
}

export type HostAccessPolicyException =
	| HostAccessRepoPolicyException
	| HostAccessExternalAgentException;

export interface HostAccessLease {
	id: string;
	root_thread_id: string;
	root_thread_title: string | null;
	state: HostAccessLeaseState;
	purpose: string;
	plan: string[];
	requested_duration_seconds: number;
	expected_resources: string | null;
	persistent_effects: string | null;
	rollback_plan: string | null;
	forbidden_effects: string | null;
	requesting_project_id: string | null;
	repo_policy_snapshot: HostAccessRepoPolicy[];
	repo_policy_hash: string;
	requested_policy_exceptions: HostAccessPolicyException[];
	application_hash: string;
	revision: number;
	decision_capability?: string;
	reviewer_model: string | null;
	reviewer: HostAccessReviewer | null;
	application_review_result: HostAccessApplicationReview | null;
	expires_at: string | null;
	terminal_reason: string | null;
	created_at: string;
	updated_at: string;
}

export interface HostAccessLeaseList {
	leases: HostAccessLease[];
}

export type HostAccessLeaseDecision = 'approve' | 'reject' | 'revoke';

export interface HostAccessLeaseListFilter {
	rootThreadId?: string;
	nonterminalOnly?: boolean;
}

export class HostAccessApiError extends Error {
	constructor(
		message: string,
		readonly status: number,
	) {
		super(message);
		this.name = 'HostAccessApiError';
	}
}

function headers(token: string, json = false): Record<string, string> {
	return {
		Authorization: `Bearer ${token}`,
		...(json ? { 'Content-Type': 'application/json' } : {}),
	};
}

async function responseJson<T>(response: Response): Promise<T> {
	if (!response.ok) {
		const body = await response.json().catch(() => null);
		const detail = (
			body
			&& typeof body === 'object'
			&& 'detail' in body
			&& typeof body.detail === 'string'
		) ? body.detail : `Host access request failed (${response.status})`;
		throw new HostAccessApiError(detail, response.status);
	}
	return response.json() as Promise<T>;
}

export async function fetchHostAccessLeases(
	token: string,
	signal?: AbortSignal,
	filter?: HostAccessLeaseListFilter,
): Promise<HostAccessLeaseList> {
	const query = new URLSearchParams();
	if (filter?.rootThreadId) query.set('root_thread_id', filter.rootThreadId);
	if (filter?.nonterminalOnly) query.set('nonterminal_only', 'true');
	const endpoint = query.size
		? `/api/host-access/leases?${query.toString()}`
		: '/api/host-access/leases';
	const response = await fetch(endpoint, {
		headers: {
			...headers(token),
			'X-CORSIA-Interactive-UI': '1',
		},
		signal,
	});
	return responseJson<HostAccessLeaseList>(response);
}

export async function decideHostAccessLease(
	token: string,
	lease: Pick<
		HostAccessLease,
		'id' | 'revision' | 'application_hash' | 'decision_capability'
	>,
	decision: HostAccessLeaseDecision,
): Promise<HostAccessLease> {
	const response = await fetch(
		`/api/host-access/leases/${encodeURIComponent(lease.id)}/${decision}`,
		{
			method: 'POST',
			headers: {
				...headers(token, true),
				'X-CORSIA-User-Decision': lease.decision_capability ?? '',
			},
			body: JSON.stringify({
				revision: lease.revision,
				application_hash: lease.application_hash,
			}),
		},
	);
	return responseJson<HostAccessLease>(response);
}
