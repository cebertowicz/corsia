import { expect, it } from 'vitest';
import { buildActivityGroups, buildResumeThreads } from '$lib/activityHome';
import type { ActivityIndexRow } from '$lib/api/plans';
import type { Thread } from '$lib/stores/threads';

it('identifies designated threads as agents in resume and plan rows', () => {
	const coordinator: Thread = { id: 'owner', user_id: 'u', profile: 'personal', title: 'Alpha Conductor', model: null, coordination: 'conductor', project_id: 'p1', created_at: '2026-08-14T12:00:00Z', updated_at: '2026-08-14T12:00:00Z' };
	const entry = {
		plan: { id: 'p', owner_kind: 'thread', owner_id: 'owner', state: 'active', revision: 1, updated_at: '2026-08-14T12:00:00Z' },
		owner: { label: 'Alpha Conductor', project_id: 'p1', thread_id: 'owner' },
		progress: { open: 1, liveness: 'advancing' }, has_nodes: true,
		display: { label_key: 'working', tone_key: 'active' }, priority_at: '2026-08-14T12:00:00Z',
	} as ActivityIndexRow;
	expect(buildResumeThreads([coordinator], {}, { projectId: 'p1' })[0].ownerType).toBe('agent');
	const groups = buildActivityGroups([entry], [coordinator], [{ id: 'p1', name: 'Alpha', status: 'active', accent_color: null }]);
	expect(groups[0].current[0].ownerType).toBe('agent');
});
