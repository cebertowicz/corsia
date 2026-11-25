import type {
	HostAccessLease,
	HostAccessItem,
	HostAccessTimed,
	HostAccessPolicyException,
	HostAccessRepoPolicy,
	HostAccessLeaseDecision,
} from '$lib/api/hostAccess';

export function text(value: unknown): value is string {
	return typeof value === 'string' && value.trim().length > 0;
}

export function nullableText(value: unknown): boolean {
	return value === null || typeof value === 'string';
}

export function validTime(value: unknown): value is string {
	return text(value) && Number.isFinite(Date.parse(value));
}

function repoPolicy(value: unknown): value is HostAccessRepoPolicy {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
	const row = value as Record<string, unknown>;
	return text(row.repo_name)
		&& text(row.repo_path)
		&& nullableText(row.pinned_branch)
		&& typeof row.protect_base_checkout === 'boolean';
}

function policyException(
	value: unknown,
	snapshot: HostAccessRepoPolicy[],
): value is HostAccessPolicyException {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
	const row = value as Record<string, unknown>;
	if (row.policy === 'external_agent') {
		return Object.keys(row).sort().join(',') === 'command,policy,reason,working_directory'
			&& text(row.reason) && row.reason.length <= 1_000
			&& text(row.command) && !row.command.includes('\0')
			&& text(row.working_directory) && row.working_directory.startsWith('/')
			&& !row.working_directory.includes('\0');
	}
	if (
		Object.keys(row).sort().join(',') !== 'policy,reason,repo_name'
		|| !text(row.repo_name)
		|| !text(row.reason)
		|| row.reason.length > 1_000
		|| (row.policy !== 'branch_pinning' && row.policy !== 'protected_base_checkout')
	) return false;
	const repo = snapshot.find((candidate) => candidate.repo_name === row.repo_name);
	return !!repo && (
		(row.policy === 'branch_pinning' && text(repo.pinned_branch))
		|| (row.policy === 'protected_base_checkout' && repo.protect_base_checkout)
	);
}

function policyContextReady(lease: HostAccessLease): boolean {
	if (!/^[0-9a-f]{64}$/.test(lease.repo_policy_hash)) return false;
	if (!Array.isArray(lease.repo_policy_snapshot)) return false;
	if (!lease.repo_policy_snapshot.every(repoPolicy)) return false;
	if (!Array.isArray(lease.requested_policy_exceptions)) return false;
	if (lease.requested_policy_exceptions.length > 8) return false;
	if (lease.requesting_project_id === null) {
		if (lease.repo_policy_snapshot.length !== 0) return false;
	} else if (!text(lease.requesting_project_id)) return false;
	const identities = new Set<string>();
	for (const exception of lease.requested_policy_exceptions) {
		if (!policyException(exception, lease.repo_policy_snapshot)) return false;
		const identity = exception.policy === 'external_agent'
			? JSON.stringify([exception.policy, exception.command, exception.working_directory])
			: JSON.stringify([exception.policy, exception.repo_name]);
		if (identities.has(identity)) return false;
		identities.add(identity);
	}
	return true;
}

export function policyLabel(policy: HostAccessPolicyException['policy']): string {
	if (policy === 'external_agent') return 'External agent';
	return policy === 'branch_pinning'
		? 'Branch pinning'
		: 'Protected base checkout';
}

export function casReady(lease: HostAccessItem | null): boolean {
	return !!lease
		&& text(lease.id)
		&& text(lease.application_hash)
		&& text(lease.decision_capability)
		&& (lease.kind === 'script' || (Number.isInteger(lease.revision) && lease.revision > 0));
}

/**
 * The single source of truth for whether a lease's decision controls
 * (Approve/Reject/Revoke) may be actionable. `decide()` uses this same
 * function for its early return, and every disabled prop derives from it,
 * so a control can never render enabled while the guard it depends on
 * would silently no-op the click.
 */
export function decisionBlocked(
	lease: HostAccessItem | null,
	stale: boolean,
	blockingLeaseId: string | null,
): boolean {
	return stale || blockingLeaseId !== null || !casReady(lease);
}

/**
 * Whether a failed read blocks this decision: Approve and Revoke, never Reject. Every reject route checks
 * the request's revision and hash on the server, so a Reject sent from the last view either ends that same
 * request or is refused as changed, and the person can always say no.
 */
export function staleBlocks(stale: boolean, decision: HostAccessLeaseDecision): boolean {
	return stale && decision !== 'reject';
}

export function approvalReady(lease: HostAccessItem | null): boolean {
	if (lease?.kind === 'fill') {
		return lease.state === 'awaiting_user' && casReady(lease)
			&& text(lease.agent) && text(lease.site) && text(lease.username)
			// The server always offers the shortest choice; a request that doesn't is not approvable, and the
			// drawer says so rather than dropping Approve with no word.
			&& Number.isInteger(lease.max_duration_seconds) && lease.max_duration_seconds >= FILL_DURATIONS_SECONDS[0]
			&& validTime(lease.created_at);
	}
	if (lease?.kind === 'script') {
		return lease.state === 'awaiting_user' && casReady(lease)
			&& text(lease.script_name) && text(lease.purpose) && text(lease.user_request)
			&& !!lease.trigger;
	}
	return !!lease
		&& lease.state === 'awaiting_user'
		&& casReady(lease)
		&& text(lease.root_thread_id)
		&& nullableText(lease.root_thread_title)
		&& text(lease.purpose)
		&& Array.isArray(lease.plan)
		&& lease.plan.length > 0
		&& lease.plan.every(text)
		&& Number.isInteger(lease.requested_duration_seconds)
		&& lease.requested_duration_seconds >= 1
		&& lease.requested_duration_seconds <= 1_800
		&& nullableText(lease.expected_resources)
		&& nullableText(lease.persistent_effects)
		&& nullableText(lease.rollback_plan)
		&& nullableText(lease.forbidden_effects)
		&& policyContextReady(lease)
		&& text(lease.reviewer?.model)
		&& nullableText(lease.reviewer?.display_name)
		&& text(lease.reviewer?.attestation)
		&& validTime(lease.created_at)
		&& validTime(lease.updated_at)
		&& (lease.expires_at === null || validTime(lease.expires_at))
		&& nullableText(lease.terminal_reason);
}

export function owner(lease: HostAccessItem): string {
	if (lease.kind === 'script') return lease.root_thread_id;
	if (lease.kind === 'fill') return text(lease.agent) ? lease.agent.trim() : 'An agent';
	return text(lease.root_thread_title)
		? lease.root_thread_title.trim()
		: (text(lease.root_thread_id) ? lease.root_thread_id : 'Unknown chat');
}

/** The pending chip's and drawer's accessible name. */
export function pendingLabel(request: HostAccessItem): string {
	if (request.kind === 'fill') return 'Review a request to sign in with a saved login';
	return request.kind === 'script'
		? 'Review permission for this script to act as your agent and use the agents’ GitHub account'
		: 'Review temporary host access';
}

export function duration(seconds: unknown): string {
	if (!Number.isInteger(seconds) || (seconds as number) < 1 || (seconds as number) > 1_800) {
		return 'Invalid duration';
	}
	if (seconds === 60) return '1 minute';
	if ((seconds as number) % 60 === 0) return `${(seconds as number) / 60} minutes`;
	return `${seconds} seconds`;
}

export function cadence(seconds: number | null): string {
	if (seconds == null) return 'When a matching event arrives';
	const [size, unit] = ([[86_400, 'day'], [3_600, 'hour'], [60, 'minute']] as const)
		.find(([size]) => seconds % size === 0) ?? [1, 'second'];
	const count = seconds / size;
	return count === 1 ? `Every ${unit}` : `Every ${count} ${unit}s`;
}

/**
 * The durations a person can give a saved-login fill: from one sign-in's worth to the
 * 30-minute cap every approval of an agent shares (the host-access lease limit). The
 * first, shortest one is the default.
 */
export const FILL_DURATIONS_SECONDS = [300, 900, 1_800] as const;

/**
 * The timed choices for one request: today's set plus the time the agent asked for, when that is
 * whole seconds within the cap, shortest first.
 */
export function fillDurations(max: number, requested: number | null = null): number[] {
	const asked = Number.isInteger(requested) && (requested as number) >= 1 && (requested as number) <= max
		? [requested as number] : [];
	return [...new Set<number>([...FILL_DURATIONS_SECONDS, ...asked])]
		.filter((seconds) => seconds <= max)
		.sort((a, b) => a - b);
}

/** Where a fill request's duration choice starts: the time the agent asked for, else the shortest. */
export function fillStartingChoice(choices: number[], requested: number | null): number | null {
	return requested !== null && choices.includes(requested) ? requested : (choices[0] ?? null);
}

/** The person's standing choice for a fill: this agent, this login, until they revoke it. */
export const UNTIL_REVOKED_LABEL = 'Until I revoke it';
/** A standing fill grant's chip, in place of a countdown. */
export const STANDING_CHIP_LABEL = 'On';

export function remaining(lease: HostAccessTimed, nowMs: number): string {
	if (lease.expires_at === null) return 'activating';
	if (!validTime(lease.expires_at)) return 'unknown';
	const total = Math.max(0, Math.ceil((Date.parse(lease.expires_at) - nowMs) / 1000));
	const minutes = Math.floor(total / 60);
	const seconds = total % 60;
	return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
