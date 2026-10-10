import type {
	HostAccessLease,
	HostAccessPolicyException,
	HostAccessRepoPolicy,
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

export function casReady(lease: HostAccessLease | null): boolean {
	return !!lease
		&& text(lease.id)
		&& text(lease.application_hash)
		&& text(lease.decision_capability)
		&& Number.isInteger(lease.revision)
		&& lease.revision > 0;
}

/**
 * The single source of truth for whether a lease's decision controls
 * (Approve/Reject/Revoke) may be actionable. `decide()` uses this same
 * function for its early return, and every disabled prop derives from it,
 * so a control can never render enabled while the guard it depends on
 * would silently no-op the click.
 */
export function decisionBlocked(
	lease: HostAccessLease | null,
	stale: boolean,
	blockingLeaseId: string | null,
): boolean {
	return stale || blockingLeaseId !== null || !casReady(lease);
}

export function approvalReady(lease: HostAccessLease | null): boolean {
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

export function owner(lease: HostAccessLease): string {
	return text(lease.root_thread_title)
		? lease.root_thread_title.trim()
		: (text(lease.root_thread_id) ? lease.root_thread_id : 'Unknown thread');
}

export function duration(seconds: unknown): string {
	if (!Number.isInteger(seconds) || (seconds as number) < 1 || (seconds as number) > 1_800) {
		return 'Invalid duration';
	}
	if (seconds === 60) return '1 minute';
	if ((seconds as number) % 60 === 0) return `${(seconds as number) / 60} minutes`;
	return `${seconds} seconds`;
}

export function remaining(lease: HostAccessLease, nowMs: number): string {
	if (lease.expires_at === null) return 'activating';
	if (!validTime(lease.expires_at)) return 'unknown';
	const total = Math.max(0, Math.ceil((Date.parse(lease.expires_at) - nowMs) / 1000));
	const minutes = Math.floor(total / 60);
	const seconds = total % 60;
	return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
