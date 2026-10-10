import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
import { approvalReady, duration } from '$lib/hostAccess/leaseFormat';
import type { HostAccessLease } from '$lib/api/hostAccess';
import { externalLease } from '../../../e2e/fixtures/external-agent-lease';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove();
});

function show(lease: HostAccessLease, stale = false) {
	component = mount(HostAccessPendingRequestDrawer, { target: host, props: {
		lease, stale, deciding: false, rejectDisabled: stale,
		approveDisabled: stale || !approvalReady(lease),
		onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(),
	} });
	flushSync();
}

function button(name: string) {
	return Array.from(host.querySelectorAll('button'))
		.find((element) => element.textContent?.trim() === name);
}

it('shows the external warning and exact launch before Plan on a projectless request', () => {
	const lease = externalLease();
	expect(approvalReady(lease)).toBe(true);
	show(lease);
	const section = host.querySelector('.policy-exceptions')!;
	expect(host.querySelector('.purpose')!.nextElementSibling).toBe(section);
	expect(section.nextElementSibling).toBe(host.querySelector('.plan'));
	expect(host.querySelector('.external-warning')!.textContent).toBe('External agents run outside CORSIA’s managed context and controls. Their instructions, skills and tools may differ, and CORSIA may not fully observe their actions, costs or results. Human approval is required for this exception, even when automatic approval is enabled.');
	const external = lease.requested_policy_exceptions[0];
	if (external.policy !== 'external_agent') throw new Error('Invalid fixture');
	expect(Array.from(section.querySelectorAll('code'), (node) => node.textContent))
		.toEqual([external.command, external.working_directory]);
	expect(button('Approve')?.disabled).toBe(false);
});

it('keeps a long requesting-thread title available through the drawer heading', () => {
	const lease = externalLease();
	lease.root_thread_title = 'External-agent lease mobile-chromium-desktop-1791378425112';
	show(lease);

	const heading = host.querySelector('.decision-head h2')!;
	expect(heading.textContent).toBe(lease.root_thread_title);
	expect(heading.getAttribute('title')).toBe(lease.root_thread_title);
});

it('escapes launch text instead of rendering HTML', () => {
	const lease = externalLease();
	const external = lease.requested_policy_exceptions[0];
	if (external.policy !== 'external_agent') throw new Error('Invalid fixture');
	external.command = "claude -p '<img src=x onerror=alert(1)>'";
	show(lease);
	expect(host.querySelector('code')?.textContent).toBe(external.command);
	expect(host.querySelector('.policy-exceptions img')).toBeNull();
});

it('retains repository-only placement and supports mixed entries without inventing a repo', () => {
	const lease = externalLease();
	lease.requesting_project_id = 'project-disposable';
	lease.repo_policy_snapshot = [{ repo_name: 'sample', repo_path: '/tmp/sample',
		pinned_branch: 'dev', protect_base_checkout: true }];
	const repository = { repo_name: 'sample', policy: 'branch_pinning' as const, reason: 'Use a task branch.' };
	lease.requested_policy_exceptions.push(repository);
	expect(approvalReady(lease)).toBe(true);
	show(lease);
	expect(host.querySelector('.policy-exceptions')!.textContent).toContain('sample: Branch pinning');
	expect(host.querySelectorAll('.external-warning')).toHaveLength(1);
});

it('keeps repository-only exceptions after repository context with no external warning', () => {
	const lease = externalLease();
	lease.requesting_project_id = 'project-disposable';
	lease.repo_policy_snapshot = [{ repo_name: 'sample', repo_path: '/tmp/sample',
		pinned_branch: 'dev', protect_base_checkout: true }];
	lease.requested_policy_exceptions = [{ repo_name: 'sample', policy: 'branch_pinning', reason: 'Use a task branch.' }];
	show(lease);
	expect(host.querySelector('.repo-policy-context')!.nextElementSibling)
		.toBe(host.querySelector('.policy-exceptions'));
	expect(host.querySelector('.external-warning')).toBeNull();
});

it('blocks an incomplete launch and shows plain missing-command copy outside code', () => {
	const lease = externalLease();
	const external = lease.requested_policy_exceptions[0];
	if (external.policy !== 'external_agent') throw new Error('Invalid fixture');
	external.command = '';
	expect(approvalReady(lease)).toBe(false);
	show(lease);
	expect(button('Approve')).toBeUndefined();
	expect(button('Reject')?.disabled).toBe(false);
	const missing = host.querySelector('.launch-missing')!;
	expect(missing.textContent).toBe('Command missing');
	expect(missing.closest('code')).toBeNull();
	expect(host.querySelector('.approval-blocked')).not.toBeNull();
});

it('rejects malformed directory, duplicate launch, extra repo field and excessive duration', () => {
	const invalidDirectory = externalLease();
	const external = invalidDirectory.requested_policy_exceptions[0];
	if (external.policy !== 'external_agent') throw new Error('Invalid fixture');
	external.working_directory = 'relative';
	expect(approvalReady(invalidDirectory)).toBe(false);
	const duplicate = externalLease();
	duplicate.requested_policy_exceptions.push({ ...duplicate.requested_policy_exceptions[0] });
	expect(approvalReady(duplicate)).toBe(false);
	const extra = externalLease();
	Object.assign(extra.requested_policy_exceptions[0], { repo_name: 'invented' });
	expect(approvalReady(extra)).toBe(false);
	const excessive = externalLease();
	excessive.requested_duration_seconds = 1_801;
	expect(approvalReady(excessive)).toBe(false);
	expect(duration(1_800)).toBe('30 minutes');
	expect(duration(1_801)).toBe('Invalid duration');
});

it('preserves stale decision guards for an otherwise complete external request', () => {
	show(externalLease(), true);
	expect(button('Approve')?.disabled).toBe(true);
	expect(button('Reject')?.disabled).toBe(true);
	expect(host.querySelector('.approval-blocked')!.textContent)
		.toContain('Refresh authoritative state before deciding.');
});
