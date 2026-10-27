import { afterEach, beforeEach, expect, expectTypeOf, it, vi } from 'vitest';
import * as api from '$lib/api/plans';
import type { ActivityIndexEnvelope, OperatorEnvelope } from '$lib/api/plans';
import { activityQuery, planKeySet, planQuery } from '$lib/stores/planQuery.svelte';
import { activityEntryFor, entryFor, operatorEntryFor, refresh, resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { token } from '$lib/stores/auth';

/** Checked by svelte-check; never invokes runes outside a component. */
export function compileQueryContracts() {
	const operator = planQuery(() => ({ kind: 'thread', id: 't' }));
	const activity = activityQuery(() => ({ kind: 'activity-workspace' }));
	expectTypeOf(operator.payload).toEqualTypeOf<OperatorEnvelope | null>();
	expectTypeOf(activity.payload).toEqualTypeOf<ActivityIndexEnvelope | null>();
	expectTypeOf(entryFor('thread:t').payload).toEqualTypeOf<OperatorEnvelope | null>();
	expectTypeOf(entryFor('activity:workspace').payload).toEqualTypeOf<ActivityIndexEnvelope | null>();
	// @ts-expect-error Arbitrary strings cannot promise a cache representation.
	entryFor('unknown');
	// @ts-expect-error Activity keys cannot promise operator detail.
	planQuery(() => ({ kind: 'activity-workspace' }));
	// @ts-expect-error Operator keys cannot promise Activity rows.
	activityQuery(() => ({ kind: 'thread', id: 't' }));
	// @ts-expect-error A detail key set excludes Activity.
	planKeySet(() => [{ kind: 'activity-workspace' }]);
	const detail = planKeySet(() => [{ kind: 'child', id: 'n' }], () => ({ kind: 'activity-workspace' }));
	expectTypeOf(detail.state({ kind: 'child', id: 'n' }).payload).toEqualTypeOf<OperatorEnvelope | null>();
	// @ts-expect-error State lookup must preserve the detail family too.
	detail.state({ kind: 'activity-workspace' });
	// @ts-expect-error Retry is correlated to the detail family.
	detail.retry({ kind: 'activity-project', id: 'p' });
	// @ts-expect-error Cache readers enforce the family.
	operatorEntryFor({ kind: 'activity-workspace' });
	// @ts-expect-error Cache readers enforce the family.
	activityEntryFor({ kind: 'workspace' });
}

beforeEach(() => { resetPlanRuntimeForTests(); token.set('test-bearer'); });
afterEach(() => vi.restoreAllMocks());

it('rejects a representation mismatch before it enters the operator cache', async () => {
	const wrong: ActivityIndexEnvelope = {
		contract_version: 1, representation: 'activity_scope_index', as_of: 'now',
		target: { kind: 'workspace_scope', id: 'w', label: 'Workspace' },
		revision_token: 'wrong', plans: [], attention: { awaiting_count: 0 },
	};
	vi.spyOn(api, 'readOperatorThread').mockResolvedValue({
		status: 'fresh', etag: 'wrong',
		// @ts-expect-error Deliberate invalid adapter result to exercise admission.
		payload: wrong,
	});
	await refresh({ kind: 'thread', id: 't' });
	const entry = operatorEntryFor({ kind: 'thread', id: 't' });
	expect(entry.status).toBe('error');
	expect(entry.payload).toBeNull();
	expect(entry.etag).toBeNull();
	expect(entry.errorMessage).toBe('Couldn’t read this plan. Reload the page.');
});
