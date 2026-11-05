import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
import { approvalReady, cadence } from '$lib/hostAccess/leaseFormat';
import type { HostAccessScript } from '$lib/api/hostAccess';

it('shows the script, trigger, purpose and request with approve and deny', async () => {
	const lease: HostAccessScript = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'capability',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	};
	const approve = vi.fn();
	const deny = vi.fn();
	const target = document.createElement('div');
	document.body.appendChild(target);
	const component = mount(HostAccessPendingRequestDrawer, { target, props: {
		lease, stale: false, deciding: false, rejectDisabled: false,
		approveDisabled: !approvalReady(lease), onApprove: approve, onReject: deny, onRetry: vi.fn(),
	} });
	flushSync();
	expect(target.textContent).toContain('task.py');
	expect(target.textContent).toContain('Allow this script to act as your agent and use the agents’ GitHub account');
	expect(target.textContent).toContain('Request (as the agent quoted it)');
	expect(target.textContent).toContain('Needs approval');
	expect(target.textContent).toContain('When it runs');
	expect(target.textContent).toContain('Every 10 minutes');
	expect(target.textContent).toContain('Check the result');
	expect(target.textContent).toContain('Please check the result');
	expect(target.textContent).toContain('/workspace/task.py');
	expect(target.textContent).toContain('Matches the request');
	const buttons = [...target.querySelectorAll('button')];
	buttons.find((button) => button.textContent?.trim() === 'Approve')?.click();
	buttons.find((button) => button.textContent?.trim() === 'Deny')?.click();
	expect(approve).toHaveBeenCalledOnce();
	expect(deny).toHaveBeenCalledOnce();
	await unmount(component);
	target.remove();
});

it('says when a script runs in plain words', () => {
	expect(cadence(null)).toBe('When a matching event arrives');
	expect(cadence(1)).toBe('Every second');
	expect(cadence(600)).toBe('Every 10 minutes');
});
