import type { ActivityIndexEnvelope } from './activityIndexTypes';

const isObject = (value: unknown): value is Record<string, unknown> =>
	!!value && typeof value === 'object' && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === 'string';
const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const isNullableString = (value: unknown): value is string | null => value === null || isString(value);

function isPlan(value: unknown): boolean {
	return isObject(value)
		&& isString(value.id)
		&& isString(value.owner_kind)
		&& isString(value.owner_id)
		&& isString(value.state)
		&& isNumber(value.revision)
		&& isString(value.updated_at);
}

function isCompactRow(value: unknown): boolean {
	if (!isObject(value) || !isPlan(value.plan) || !isObject(value.owner)) return false;
	if (!isString(value.owner.label) || !isNullableString(value.owner.project_id)
		|| !isNullableString(value.owner.thread_id)) return false;
	if (!isObject(value.progress) || !isNumber(value.progress.open)
		|| !isString(value.progress.liveness)) return false;
	if (typeof value.has_nodes !== 'boolean' || !isObject(value.display)) return false;
	return isString(value.display.label_key)
		&& isString(value.display.tone_key)
		&& isString(value.priority_at);
}

function isRow(value: unknown): boolean {
	return isCompactRow(value) && !Object.hasOwn(value, 'subagents');
}

export function decodeActivityIndexEnvelope(value: unknown): ActivityIndexEnvelope | null {
	if (!isObject(value) || value.contract_version !== 1
		|| value.representation !== 'activity_scope_index') return null;
	if (!isString(value.as_of) || !isString(value.revision_token) || !isObject(value.target)) return null;
	if (!isString(value.target.kind) || !isString(value.target.id) || !isString(value.target.label)) return null;
	if (!isObject(value.attention) || !isNumber(value.attention.awaiting_count)) return null;
	if (!Array.isArray(value.plans) || !value.plans.every(isRow)) return null;
	return value as unknown as ActivityIndexEnvelope;
}
