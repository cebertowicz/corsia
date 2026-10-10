import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	decideHostAccessLease,
	fetchHostAccessLeases,
	type HostAccessLease,
} from './hostAccess';

const LEASE: HostAccessLease = {
	id: 'lease-1',
	root_thread_id: 'thread-1',
	root_thread_title: 'Home Assistant',
	state: 'awaiting_user',
	purpose: 'Repair the media bridge.',
	plan: ['Inspect the service', 'Apply the bounded repair'],
	requested_duration_seconds: 300,
	expected_resources: 'Home Assistant host',
	persistent_effects: 'One configuration update',
	rollback_plan: 'Restore the prior file',
	forbidden_effects: 'No unrelated services',
	requesting_project_id: 'project-a',
	repo_policy_snapshot: [{
		repo_name: 'corsia',
		repo_path: '/srv/corsia',
		pinned_branch: 'dev',
		protect_base_checkout: true,
	}],
	repo_policy_hash: 'b'.repeat(64),
	requested_policy_exceptions: [{
		repo_name: 'corsia',
		policy: 'branch_pinning',
		reason: 'Emergency repair',
	}],
	application_hash: 'hash-1',
	revision: 4,
	decision_capability: 'decision-1',
	reviewer_model: 'anthropic/claude-opus',
	reviewer: {
		model: 'anthropic/claude-opus',
		display_name: 'Claude Opus',
		attestation: 'The request is bounded and matches the plan.',
	},
	application_review_result: {
		allowed: true,
		reason: 'The work is bounded.',
		attestation: 'The request is bounded and matches the plan.',
	},
	expires_at: null,
	terminal_reason: null,
	created_at: '2026-07-18T16:00:00+00:00',
	updated_at: '2026-07-18T16:01:00+00:00',
};

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('host access API', () => {
	it('loads lease truth with a Bearer header', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ leases: [LEASE] }),
		});
		vi.stubGlobal('fetch', fetchMock);

		const result = await fetchHostAccessLeases('token-1');

		expect(result.leases).toEqual([LEASE]);
		expect(fetchMock).toHaveBeenCalledWith('/api/host-access/leases', {
			headers: {
				Authorization: 'Bearer token-1',
				'X-CORSIA-Interactive-UI': '1',
			},
			signal: undefined,
		});
	});

	it("requests only the selected root's nonterminal leases when asked", async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ leases: [LEASE] }),
		});
		vi.stubGlobal('fetch', fetchMock);

		await fetchHostAccessLeases('token-1', undefined, {
			rootThreadId: 'thread / 1',
			nonterminalOnly: true,
		});

		expect(fetchMock).toHaveBeenCalledWith(
			'/api/host-access/leases?root_thread_id=thread+%2F+1&nonterminal_only=true',
			{
				headers: {
					Authorization: 'Bearer token-1',
					'X-CORSIA-Interactive-UI': '1',
				},
				signal: undefined,
			},
		);
	});

	it.each(['approve', 'reject', 'revoke'] as const)(
		'sends the exact optimistic-concurrency body for %s',
		async (decision) => {
			const fetchMock = vi.fn().mockResolvedValue({
				ok: true,
				json: async () => ({ ...LEASE, state: 'active' }),
			});
			vi.stubGlobal('fetch', fetchMock);

			await decideHostAccessLease('token-1', LEASE, decision);

			expect(fetchMock).toHaveBeenCalledWith(
				`/api/host-access/leases/lease-1/${decision}`,
				{
					method: 'POST',
					headers: {
						Authorization: 'Bearer token-1',
						'Content-Type': 'application/json',
						'X-CORSIA-User-Decision': 'decision-1',
					},
					body: JSON.stringify({
						revision: 4,
						application_hash: 'hash-1',
					}),
				},
			);
		},
	);

	it('preserves 409 status so the caller can refresh authoritative truth', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: false,
			status: 409,
			json: async () => ({ detail: 'lease changed' }),
		}));

		const error = await decideHostAccessLease(
			'token-1',
			LEASE,
			'approve',
		).catch((caught) => caught);

		expect(error).toMatchObject({
			message: 'lease changed',
			status: 409,
		});
	});
});
