import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
import { approvalReady } from '$lib/hostAccess/leaseFormat';
import { externalLease } from '../../../e2e/fixtures/external-agent-lease';

it('shows the script, trigger, purpose and request with approve and deny', async () => {
	const lease = externalLease();
	lease.kind = 'script';
	lease.script_name = 'task.py';
	lease.trigger = { cadence_seconds: 600 };
	lease.purpose = 'Check the result';
	lease.user_request = 'Please check the result';
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
	expect(target.textContent).toContain('Every 600 seconds');
	expect(target.textContent).toContain('Check the result');
	expect(target.textContent).toContain('Please check the result');
	const buttons = [...target.querySelectorAll('button')];
	buttons.find((button) => button.textContent?.trim() === 'Approve')?.click();
	buttons.find((button) => button.textContent?.trim() === 'Deny')?.click();
	expect(approve).toHaveBeenCalledOnce();
	expect(deny).toHaveBeenCalledOnce();
	await unmount(component);
	target.remove();
});
