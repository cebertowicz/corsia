import type { HostAccessLease } from '../../src/lib/api/hostAccess';

/** Disposable presentation fixture; never submits or grants host access. */
export function externalLease(rootThreadId = 'thread-external'): HostAccessLease {
	return {
		id: 'external-request', root_thread_id: rootThreadId, root_thread_title: 'Research task',
		state: 'awaiting_user',
		purpose: 'Run an independent analysis of a disposable sample using an external agent.',
		plan: ['Run the approved command in the sample directory.', 'Inspect its result before using it.'],
		requested_duration_seconds: 1_800,
		expected_resources: 'A disposable sample report.', persistent_effects: 'None.',
		rollback_plan: 'Remove disposable output.', forbidden_effects: 'No unrelated access.',
		requesting_project_id: null, repo_policy_snapshot: [], repo_policy_hash: 'b'.repeat(64),
		requested_policy_exceptions: [{
			policy: 'external_agent', reason: 'Use a separate harness to analyze the sample.',
			command: "claude -p 'Analyze the sample report in this directory' --add-dir /tmp/"
				+ 'independent_sample_'.repeat(8),
			working_directory: '/tmp/corsia-external-sample/' + 'disposable-analysis-inputs-'.repeat(7),
		}],
		application_hash: 'a'.repeat(64), revision: 1, decision_capability: 'disposable-decision',
		reviewer_model: 'mock/test-model',
		reviewer: { model: 'mock/test-model', display_name: 'Independent reviewer',
			attestation: 'The request is bounded and awaits a human decision.' },
		application_review_result: { allowed: true, approval_mode: 'manual' },
		expires_at: null, terminal_reason: null,
		created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-01T12:00:00Z',
	};
}
