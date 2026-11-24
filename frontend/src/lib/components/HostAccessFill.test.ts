import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
import HostAccessPendingTrigger from './HostAccessPendingTrigger.svelte';
import HostAccessActiveLeaseDrawer from './HostAccessActiveLeaseDrawer.svelte';
import HostAccessActiveLeaseChip from './HostAccessActiveLeaseChip.svelte';
import { approvalReady, fillDurations, owner, pendingLabel } from '$lib/hostAccess/leaseFormat';
import { decideHostAccessLease, type HostAccessFill, type HostAccessItem } from '$lib/api/hostAccess';

vi.mock('$lib/api/request', () => ({ apiFetch: vi.fn() }));

const targets: HTMLElement[] = [];
afterEach(() => { for (const target of targets.splice(0)) target.remove(); });

function fill(overrides: Partial<HostAccessFill> = {}): HostAccessFill {
	return {
		id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'agent', root_thread_id: 'agent',
		agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
		revision: 1, application_hash: 'a'.repeat(64), decision_capability: 'capability',
		max_duration_seconds: 1800, duration_seconds: null, expires_at: null,
		created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
		...overrides,
	};
}

function target(): HTMLElement {
	const element = document.createElement('div');
	document.body.appendChild(element);
	targets.push(element);
	return element;
}

function pending(request: HostAccessFill, props: Record<string, unknown> = {}) {
	const host = target();
	const onApprove = vi.fn();
	const onReject = vi.fn();
	const component = mount(HostAccessPendingRequestDrawer, { target: host, props: {
		lease: request, stale: false, deciding: false, rejectDisabled: false,
		approveDisabled: !approvalReady(request), onApprove, onReject, onRetry: vi.fn(), ...props,
	} });
	flushSync();
	return { host, component, onApprove, onReject };
}

it('names the agent, site and username, and approves for the duration the person picks', async () => {
	const { host, component, onApprove, onReject } = pending(fill());
	const text = host.textContent ?? '';
	expect(text).toContain('Sign in with a saved login');
	expect(text).toContain('Bookkeeper');
	expect(text).toContain('https://example.com');
	expect(text).toContain('me@example.com');
	expect(text).toContain('Needs approval');
	expect(text).toContain('The agent never sees it, and you can revoke this approval at any time.');
	const radios = [...host.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
	expect(radios.map((radio) => radio.textContent?.trim())).toEqual(['5 minutes', '15 minutes', '30 minutes']);
	expect(radios[0].getAttribute('aria-checked')).toBe('true');
	radios[1].click();
	flushSync();
	expect(radios[1].getAttribute('aria-checked')).toBe('true');
	expect(radios[0].getAttribute('aria-checked')).toBe('false');
	const buttons = [...host.querySelectorAll('button')];
	buttons.find((button) => button.textContent?.trim() === 'Approve')?.click();
	expect(onApprove).toHaveBeenCalledOnce();
	expect(onApprove.mock.calls[0][1]).toBe(900);
	const reject = buttons.find((button) => button.textContent?.trim() === 'Reject')!;
	reject.click();
	expect(onReject).toHaveBeenCalledOnce();
	// Approve is the one solid action; Reject is a text action right beside it.
	const approve = buttons.find((button) => button.textContent?.trim() === 'Approve')!;
	expect(approve.classList.contains('primary')).toBe(true);
	expect(reject.classList.contains('ghost')).toBe(true);
	expect(reject.nextElementSibling).toBe(approve);
	expect(buttons.filter((button) => button.classList.contains('primary'))).toHaveLength(1);
	await unmount(component);
});

it('moves the duration choice with the arrow keys', async () => {
	const { host, component, onApprove } = pending(fill());
	const first = host.querySelector<HTMLButtonElement>('[role="radio"]')!;
	first.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
	flushSync();
	expect(host.querySelector('[data-seconds="1800"]')?.getAttribute('aria-checked')).toBe('true');
	[...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Approve')?.click();
	expect(onApprove.mock.calls[0][1]).toBe(1800);
	await unmount(component);
});

it('offers nothing above the cap the server sends', () => {
	expect(fillDurations(1800)).toEqual([300, 900, 1800]);
	expect(fillDurations(900)).toEqual([300, 900]);
	expect(fillDurations(0)).toEqual([]);
});

it('cannot be approved when stale or incomplete, and says why', async () => {
	const stale = pending(fill(), { stale: true, approveDisabled: true });
	expect(stale.host.querySelector('.decision-head .status')?.textContent?.trim()).toBe('Couldn’t check');
	expect(stale.host.textContent).toContain('Couldn’t check that this request is still current, so you can’t approve it yet.');
	expect(stale.host.textContent).not.toContain('Retry to check again');
	await unmount(stale.component);
	// Without its decision fields neither decision can be sent (the control disables Reject too).
	const incomplete = pending(fill({ decision_capability: '' }), { rejectDisabled: true });
	expect(incomplete.host.querySelector('.blocked-line')?.textContent?.trim()).toBe('This request is missing details CORSIA needs to check it, so you can’t approve or reject it. It clears when CORSIA restarts.');
	const buttons = [...incomplete.host.querySelectorAll<HTMLButtonElement>('.decision-buttons button')];
	// Approve is shown disabled, never hidden, so Reject keeps its place; reading again won't help, so no Retry.
	expect(buttons.map((button) => [button.textContent?.trim(), button.disabled])).toEqual([['Reject', true], ['Approve', true]]);
	expect([...incomplete.host.querySelectorAll('button')].map((button) => button.textContent?.trim())).not.toContain('Retry');
	await unmount(incomplete.component);
	// Below the shortest duration there is nothing to pick: Approve is off, and the drawer says why.
	const short = pending(fill({ max_duration_seconds: 120 }));
	expect(short.host.querySelector('.blocked-line')?.textContent?.trim()).toBe('This request is missing details CORSIA needs to check it, so you can’t approve it. Reject it and the agent can ask again.');
	expect([...short.host.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.trim() === 'Approve')?.disabled).toBe(true);
	await unmount(short.component);
	expect(approvalReady(fill({ max_duration_seconds: 299 }))).toBe(false);
	expect(approvalReady(fill({ max_duration_seconds: 300 }))).toBe(true);
	expect(approvalReady(fill({ site: '' }))).toBe(false);
	expect(approvalReady(fill({ state: 'active' }))).toBe(false);
});

it('draws the lease and script entries as the same plain line: a sentence-case kind, no pill, the status as text', () => {
	const lease = { id: 'lease-1', kind: 'lease', state: 'awaiting_user', root_thread_id: 'agent',
		root_thread_title: 'Bookkeeper', requested_duration_seconds: 900 } as unknown as HostAccessItem;
	const script = { id: 'script-1', kind: 'script', state: 'awaiting_user', root_thread_id: 'agent',
		script_name: 'check.py' } as unknown as HostAccessItem;
	for (const [request, kind, name] of [[lease, 'Temporary host access', 'Bookkeeper'], [script, 'Script approval', 'check.py']] as const) {
		for (const stale of [false, true]) {
			const host = target();
			const component = mount(HostAccessPendingTrigger, { target: host, props: { request, stale, open: false, onclick: vi.fn() } });
			flushSync();
			expect(host.querySelector('.pending-kind')?.textContent).toBe(kind);
			expect(host.querySelector('.pending-owner')?.textContent).toBe(name);
			expect(host.querySelector('.pending-status')?.textContent).toBe(stale ? 'Couldn’t check' : 'Needs approval');
			expect(host.querySelector('.pending-action')?.textContent).toBe('Review →');
			expect(host.querySelector('.pill, .eyebrow, .pending-site')).toBeNull();
			expect(host.textContent).not.toContain('15 minutes');
			void unmount(component);
		}
	}
});

it('shows a pending fill on the strip as the agent and site, with status as text', async () => {
	const host = target();
	const onclick = vi.fn();
	const component = mount(HostAccessPendingTrigger, { target: host, props: {
		request: fill(), stale: false, open: false, onclick,
	} });
	flushSync();
	const button = host.querySelector('button')!;
	expect(button.getAttribute('aria-label')).toBe('Review a request to sign in with a saved login');
	expect(pendingLabel(fill())).toBe('Review a request to sign in with a saved login');
	expect(button.textContent).toContain('Saved login');
	expect(button.querySelector('.pending-owner')?.textContent).toBe('Bookkeeper');
	expect(button.querySelector('.pending-site')?.getAttribute('title')).toBe(fill().site);
	// The entry shows the site without its scheme; the title and the drawer keep the full address.
	expect(button.querySelector('.pending-site')?.textContent).toBe(fill().site.replace(/^https:\/\//, ''));
	expect(button.querySelector('.pending-site')?.textContent).not.toContain('://');
	expect(button.textContent).toContain('Needs approval');
	expect(host.querySelector('.pill')).toBeNull();
	button.click();
	expect(onclick).toHaveBeenCalledOnce();
	await unmount(component);
	expect(owner(fill({ agent: ' ' }))).toBe('An agent');
});

it('shows a live grant with its time left and revokes it', async () => {
	const host = target();
	const onRevoke = vi.fn();
	const active = fill({
		state: 'active', revision: 2, duration_seconds: 900, expires_at: '2026-01-01T00:15:00Z',
	});
	const component = mount(HostAccessActiveLeaseDrawer, { target: host, props: {
		lease: active, nowMs: Date.parse('2026-01-01T00:05:00Z'), disabled: false, deciding: false, onRevoke,
	} });
	flushSync();
	const text = host.textContent ?? '';
	expect(text).toContain('CORSIA types the password into this site for the agent. The agent never sees it.');
	expect(text).not.toContain('can sign in to');
	expect(text).toContain('15 minutes');
	expect(text).toContain('10:00');
	expect(text).not.toContain('Reviewer');
	const revoke = [...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Revoke approval');
	expect(revoke?.classList.contains('ghost')).toBe(true);
	revoke?.click();
	expect(onRevoke).toHaveBeenCalledOnce();
	await unmount(component);
});

it('labels a live grant chip as a saved login, not host access', async () => {
	const host = target();
	const component = mount(HostAccessActiveLeaseChip, { target: host, props: {
		lease: fill({ state: 'active', revision: 2, duration_seconds: 300, expires_at: '2026-01-01T00:05:00Z' }),
		nowMs: Date.parse('2026-01-01T00:00:00Z'), open: false, deciding: false, disabled: false,
		onOpen: vi.fn(), onClose: vi.fn(), onRevoke: vi.fn(),
	} });
	flushSync();
	const trigger = host.querySelector('.active-chip-trigger')!;
	// Its words are its name, time included: no static label, nothing hidden, no decorative icon.
	expect(trigger.hasAttribute('aria-label')).toBe(false);
	expect(trigger.textContent?.replace(/\s+/g, ' ').trim()).toBe('Saved login · 5 min left');
	expect(trigger.querySelector('[aria-hidden="true"]')).toBeNull();
	expect(trigger.querySelector('svg')).toBeNull();
	await unmount(component);
});

it('posts a fill decision to the fill route with the picked duration only on approve', async () => {
	const { apiFetch } = await import('$lib/api/request');
	const fetch = vi.mocked(apiFetch);
	fetch.mockResolvedValue(new Response(JSON.stringify({}), { status: 200 }));
	await decideHostAccessLease(fill(), 'approve', 900);
	expect(fetch.mock.calls[0][0]).toBe('/api/host-access/leases/fills/fill-1/approve');
	const init = fetch.mock.calls[0][1] as RequestInit;
	expect(JSON.parse(init.body as string)).toEqual({
		revision: 1, application_hash: 'a'.repeat(64), duration_seconds: 900,
	});
	expect((init.headers as Record<string, string>)['X-CORSIA-User-Decision']).toBe('capability');
	fetch.mockResolvedValue(new Response(JSON.stringify({}), { status: 200 }));
	await decideHostAccessLease(fill({ state: 'active', revision: 2 }), 'revoke');
	expect(fetch.mock.calls[1][0]).toBe('/api/host-access/leases/fills/fill-1/revoke');
	expect(JSON.parse((fetch.mock.calls[1][1] as RequestInit).body as string)).toEqual({
		revision: 2, application_hash: 'a'.repeat(64),
	});
});
