import { describe, expect, it } from 'vitest';
import { decodeActivityIndexEnvelope } from '$lib/api/activityIndexDecode';

function envelope() {
	return {
		contract_version: 1,
		representation: 'activity_scope_index',
		as_of: '2026-09-30T00:00:00Z',
		target: { kind: 'workspace_scope', id: 'workspace', label: 'Workspace' },
		revision_token: 'activity-r1',
		plans: [{
			plan: {
				id: 'plan-1', owner_kind: 'thread', owner_id: 'thread-1',
				state: 'active', revision: 3, updated_at: '2026-09-30T00:00:00Z',
			},
			owner: { label: 'Thread 1', project_id: 'project-1', thread_id: 'thread-1' },
			progress: { open: 2, liveness: 'advancing' },
			has_nodes: true,
			display: { label_key: 'advancing', tone_key: 'active' },
			priority_at: '2026-09-30T00:00:00Z',
		}],
		attention: { awaiting_count: 1 },
	};
}

describe('decodeActivityIndexEnvelope', () => {
	it('admits the compact contract and an empty scope', () => {
		expect(decodeActivityIndexEnvelope(envelope())).not.toBeNull();
		expect(decodeActivityIndexEnvelope({ ...envelope(), plans: [] })).not.toBeNull();
	});

	it('requires every compact row fact rendered or ordered by Activity', () => {
		const body = envelope();
		for (const broken of [
			{ ...body, plans: [{ ...body.plans[0], has_nodes: undefined }] },
			{ ...body, plans: [{ ...body.plans[0], priority_at: undefined }] },
			{ ...body, plans: [{ ...body.plans[0], progress: { open: 2 } }] },
			{ ...body, plans: [{ ...body.plans[0], display: { label_key: 'advancing' } }] },
		]) expect(decodeActivityIndexEnvelope(broken)).toBeNull();
	});

	it('does not admit the full v2 representation', () => {
		expect(decodeActivityIndexEnvelope({
			...envelope(), contract_version: 2, representation: undefined,
		})).toBeNull();
	});
});
