/** Pure identity and invalidation matching for contextual plan reads. */

export type OperatorKey =
	| { kind: 'workspace' }
	| { kind: 'project'; id: string }
	| { kind: 'thread'; id: string }
	| { kind: 'child'; id: string };
export type ActivityKey =
	| { kind: 'activity-workspace' }
	| { kind: 'activity-project'; id: string };
export type PlanKey = OperatorKey | ActivityKey;

export function isActivityKey(key: PlanKey): key is ActivityKey {
	return key.kind === 'activity-workspace' || key.kind === 'activity-project';
}

export type OperatorKeyString = 'workspace' | `project:${string}` | `thread:${string}` | `child:${string}`;
export type ActivityKeyString = 'activity:workspace' | `activity-project:${string}`;
export type PlanKeyString = OperatorKeyString | ActivityKeyString;

export function planKeyString(key: OperatorKey): OperatorKeyString;
export function planKeyString(key: ActivityKey): ActivityKeyString;
export function planKeyString(key: PlanKey): PlanKeyString;
export function planKeyString(key: PlanKey): PlanKeyString {
	if (key.kind === 'workspace') return 'workspace';
	if (key.kind === 'activity-workspace') return 'activity:workspace';
	return `${key.kind}:${key.id}`;
}

export function parseKeyString(keyStr: string): PlanKey | null {
	if (keyStr === 'workspace') return { kind: 'workspace' };
	if (keyStr === 'activity:workspace') return { kind: 'activity-workspace' };
	const [kind, ...rest] = keyStr.split(':');
	const id = rest.join(':');
	if ((kind === 'project' || kind === 'thread' || kind === 'child'
		|| kind === 'activity-project') && id) {
		return { kind, id } as PlanKey;
	}
	return null;
}

export interface PlanChange {
	plan_id: string;
	revision: number;
	owner: { kind: string; id: string };
	scope: { workspace_id: string | null; project_id: string | null };
}

export interface PlansChangedMessage {
	type: 'plans_changed';
	contract_version: number;
	plans: PlanChange[];
}

/** A child key matches by the owner thread learned from its fetched payload. */
export function keyMatchesChange(
	key: PlanKey,
	ownerThreadId: string | null,
	change: PlanChange,
): boolean {
	if (key.kind === 'workspace' || key.kind === 'activity-workspace') return true;
	if (key.kind === 'project' || key.kind === 'activity-project') {
		return change.scope.project_id === key.id;
	}
	if (key.kind === 'thread') {
		return change.owner.kind === 'thread' && change.owner.id === key.id;
	}
	return change.owner.kind === 'thread'
		&& ownerThreadId != null
		&& change.owner.id === ownerThreadId;
}
