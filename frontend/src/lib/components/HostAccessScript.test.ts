import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
import HostAccessPendingTrigger from './HostAccessPendingTrigger.svelte';
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

it('names a stale script request as the lease names it', async () => {
	const lease: HostAccessScript = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'capability',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	};
	const target = document.createElement('div');
	document.body.appendChild(target);
	const component = mount(HostAccessPendingRequestDrawer, { target, props: {
		lease, stale: true, deciding: false, rejectDisabled: false, approveDisabled: true,
		onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(),
	} });
	flushSync();
	expect(target.textContent).toContain('Couldn’t check');
	expect(target.textContent).not.toContain('Needs refresh');
	expect(target.textContent).toContain('Couldn’t check that this request is still current');
	expect(target.querySelector('.pill')).toBeNull();
	await unmount(component);
	target.remove();
});

it('says an incomplete script request cannot be approved, and names Deny', async () => {
	const base = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'decision',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	} satisfies HostAccessScript;
	const cases = [
		// Its decision fields are whole: Deny still works.
		[{ ...base, purpose: '' }, false, 'This request is missing details CORSIA needs to check it, so you can’t approve it. Deny it and the agent can ask again.'],
		// Without them neither decision can be sent; the control disables Deny too.
		[{ ...base, decision_capability: '' }, true, 'This request is missing details CORSIA needs to check it, so you can’t approve or deny it. It stays here until the agent withdraws it or the agent is stopped.'],
	] as const;
	for (const [lease, rejectDisabled, line] of cases) {
		const target = document.createElement('div');
		document.body.appendChild(target);
		const component = mount(HostAccessPendingRequestDrawer, { target, props: {
			lease, stale: false, deciding: false, rejectDisabled, approveDisabled: false,
			onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(),
		} });
		flushSync();
		expect(target.querySelector('.blocked-line')?.textContent?.trim()).toBe(line);
		const buttons = [...target.querySelectorAll<HTMLButtonElement>('button')];
		expect(buttons.map((button) => [button.textContent?.trim(), button.disabled])).toEqual([['Deny', rejectDisabled], ['Approve', true]]);
		await unmount(component);
		target.remove();
	}
});

it('hides the Purpose label when the purpose is empty, in every state of the script drawer', async () => {
	const base = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'decision',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	} satisfies HostAccessScript;
	const labels = (target: HTMLElement) =>
		[...target.querySelectorAll('.decision-body .section-label')].map((label) => label.textContent?.trim());
	const cases = [
		// [request, stale, rejectDisabled, Purpose label shown]
		[base, false, false, true],
		[base, true, false, true],
		[{ ...base, purpose: '' }, false, false, false],
		[{ ...base, purpose: '   ' }, false, false, false],
		[{ ...base, purpose: '' }, true, false, false],
		[{ ...base, purpose: '', decision_capability: '' }, false, true, false],
	] as const;
	for (const [lease, stale, rejectDisabled, shown] of cases) {
		const target = document.createElement('div');
		document.body.appendChild(target);
		const component = mount(HostAccessPendingRequestDrawer, { target, props: {
			lease, stale, deciding: false, rejectDisabled, approveDisabled: false,
			onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(),
		} });
		flushSync();
		expect(labels(target)).toEqual(shown
			? ['File', 'When it runs', 'Purpose', 'Request (as the agent quoted it)', 'Reviewer’s reason']
			: ['File', 'When it runs', 'Request (as the agent quoted it)', 'Reviewer’s reason']);
		expect(target.querySelector('.decision-body .purpose') !== null).toBe(shown);
		await unmount(component);
		target.remove();
	}
});

it('puts Deny beside Approve as Reject sits in the other drawers: a ghost text action, no red, no spacer between', async () => {
	const lease: HostAccessScript = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'capability',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	};
	for (const rejectDisabled of [false, true]) {
		const target = document.createElement('div');
		document.body.appendChild(target);
		const component = mount(HostAccessPendingRequestDrawer, { target, props: {
			lease, stale: false, deciding: false, rejectDisabled, approveDisabled: false,
			onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(),
		} });
		flushSync();
		const row = [...target.querySelector('.decision-buttons')!.children];
		expect(row.map((child) => child.classList.contains('decision-spacer') ? 'spacer' : child.textContent?.trim()))
			.toEqual(['spacer', 'Deny', 'Approve']);
		expect(row[1].classList.contains('ghost')).toBe(true);
		expect(row[1].classList.contains('danger')).toBe(false);
		expect((row[1] as HTMLButtonElement).disabled).toBe(rejectDisabled);
		await unmount(component);
		target.remove();
	}
});

it('says when a script runs in plain words', () => {
	expect(cadence(null)).toBe('When a matching event arrives');
	expect(cadence(1)).toBe('Every second');
	expect(cadence(600)).toBe('Every 10 minutes');
});

it('shows a script chip status as text, not a pill', async () => {
	const request: HostAccessScript = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'capability',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	};
	const onclick = vi.fn();
	const target = document.createElement('div');
	document.body.appendChild(target);
	const component = mount(HostAccessPendingTrigger, { target, props: { request, stale: false, open: false, onclick } });
	flushSync();
	const button = target.querySelector('button')!;
	expect(button.getAttribute('aria-label')).toBe('Review permission for this script to act as your agent and use the agents\u2019 GitHub account');
	expect(button.textContent).toContain('Script approval');
	expect(button.textContent).toContain('task.py');
	expect(button.textContent).toContain('Needs approval');
	expect(target.querySelector('.pill')).toBeNull();
	button.click();
	expect(onclick).toHaveBeenCalledOnce();
	await unmount(component);
	target.remove();
});

it('keeps a submitted script decision visible on the closed chip', async () => {
	const request: HostAccessScript = {
		id: 'script', kind: 'script', state: 'awaiting_user', thread_id: 'owner', root_thread_id: 'owner',
		application_hash: 'a'.repeat(64), decision_capability: 'capability',
		script_name: 'task.py', script_path: '/workspace/task.py',
		trigger: { cadence_seconds: 600 }, purpose: 'Check the result',
		user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
	};
	const target = document.createElement('div');
	document.body.appendChild(target);
	const component = mount(HostAccessPendingTrigger, { target, props: {
		request, stale: false, deciding: true, open: false, onclick: vi.fn(),
	} });
	flushSync();
	const button = target.querySelector('button')!;
	expect(button.textContent).toContain('Saving…');
	expect(button.textContent).not.toContain('Needs approval');
	expect(button.getAttribute('aria-busy')).toBe('true');
	await unmount(component);
	target.remove();
});
