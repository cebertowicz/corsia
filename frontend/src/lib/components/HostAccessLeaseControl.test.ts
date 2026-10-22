import { flushSync, mount, tick, unmount } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HostAccessLeaseControl from './HostAccessLeaseControl.svelte';
import { token } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';
import type { HostAccessLease } from '$lib/api/hostAccess';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const waitingLease: HostAccessLease = {
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
		reason: 'The emergency repair must land on main.',
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

function response(body: unknown, ok = true, status = 200) {
	return {
		ok,
		status,
		json: async () => body,
	} as Response;
}

function mountControl(target = host, threadId: string | null = 'thread-1') {
	return mount(HostAccessLeaseControl, {
		target,
		props: { threadId },
	});
}

function activeLease(patch: Partial<HostAccessLease> = {}): HostAccessLease {
	return {
		...waitingLease,
		state: 'active',
		revision: 5,
		expires_at: '2099-07-18T16:06:00.000Z',
		...patch,
	};
}

function addComposer(): HTMLElement {
	const composer = document.createElement('div');
	composer.setAttribute('role', 'textbox');
	composer.setAttribute('aria-label', 'Message CORSIA');
	composer.tabIndex = 0;
	host.appendChild(composer);
	return composer;
}

async function openPendingDrawer(): Promise<HTMLElement> {
	const trigger = host.querySelector<HTMLButtonElement>('[aria-label="Review temporary host access"]')!;
	trigger.click();
	flushSync();
	await tick();
	return host.querySelector('[role="dialog"]')!;
}

async function openActiveDrawer(): Promise<HTMLElement> {
	const trigger = host.querySelector<HTMLButtonElement>('.active-chip [aria-label="Host access"]')!;
	trigger.click();
	flushSync();
	await tick();
	return host.querySelector('[role="dialog"]')!;
}

async function openRevokeButton(): Promise<HTMLButtonElement> {
	const dialog = await openActiveDrawer();
	return Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
		.find((button) => button.textContent?.trim() === 'Revoke host access')!;
}

beforeEach(() => {
	token.set('test-token');
	// The drawer portals to document.body, so the host is the body itself.
	host = document.body;
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
		})) as unknown as typeof Element.prototype.animate;
	}
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.replaceChildren();
	token.set(null);
	vi.useRealTimers();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('HostAccessLeaseControl', () => {
	it('shows the full reviewed questionnaire and performs one explicit approval', async () => {
		let state: HostAccessLease = waitingLease;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				state = {
					...waitingLease,
					state: 'active',
					revision: 5,
					expires_at: '2099-07-18T16:06:00+00:00',
				};
				return response(state);
			}
			return response({ leases: [state] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		expect(fetchMock.mock.calls[0]?.[0]).toBe(
			'/api/host-access/leases?root_thread_id=thread-1&nonterminal_only=true',
		);
		expect(host.querySelector('[role="alertdialog"]')).toBeNull();
		expect(host.textContent).not.toContain('Repair the media bridge.');

		const dialog = await openPendingDrawer();
		expect(dialog.textContent).toContain('Home Assistant');
		expect(dialog.textContent).toContain('Repair the media bridge.');
		expect(
			Array.from(dialog.querySelectorAll('.plan li')).map((item) => item.textContent),
		).toEqual(['Inspect the service', 'Apply the bounded repair']);
		expect(dialog.textContent).toContain('5 minutes');
		expect(dialog.textContent).toContain('Home Assistant host');
		expect(dialog.textContent).toContain('One configuration update');
		expect(dialog.textContent).toContain('Restore the prior file');
		expect(dialog.textContent).toContain('No unrelated services');
		expect(dialog.textContent).toContain('Repository policy context');
		expect(dialog.textContent).toContain('corsia');
		expect(dialog.textContent).toContain('Pinned to dev');
		expect(dialog.textContent).toContain('Policy exceptions requested');
		expect(dialog.textContent).toContain('Branch pinning');
		expect(dialog.textContent).toContain('The emergency repair must land on main.');
		expect(dialog.textContent).toContain('Claude Opus');
		expect(dialog.textContent).toContain('The request is bounded and matches the plan.');

		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		expect(approve?.disabled).toBe(false);
		approve?.click();
		await tick();

		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
		expect(post?.[0]).toBe('/api/host-access/leases/lease-1/approve');
		expect(JSON.parse(String(post?.[1]?.body))).toEqual({
			revision: 4,
			application_hash: 'hash-1',
		});
	});

	it('collapses active host access behind a stable compact disclosure', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-07-18T16:00:00.000Z'));
		const fetchMock = vi.fn().mockResolvedValue(response({
			leases: [activeLease({
				expires_at: '2026-07-18T16:05:00.000Z',
				application_review_result: {
					approval_mode: 'automatic',
				},
			})],
		}));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.advanceTimersByTimeAsync(0);
		await tick();

		const chip = host.querySelector('.active-chip');
		expect(chip).toBeTruthy();
		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
		const trigger = chip!.querySelector<HTMLButtonElement>('[aria-label="Host access"]');
		expect(trigger?.getAttribute('aria-label')).toBe('Host access');
		expect(trigger?.querySelector('.host-access-icon')).toBeTruthy();
		expect(trigger?.textContent).toContain('5:00');
		expect(trigger?.querySelector('.countdown')?.getAttribute('aria-hidden')).toBe('true');
		expect(host.textContent).not.toContain('Home Assistant');

		trigger?.click();
		flushSync();
		await tick();
		const dialog = host.querySelector('[role="dialog"]')!;
		expect(dialog.textContent).toContain('Automatic reviewer approval');

		dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		await tick();

		const exposedRevoke = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Revoke host access');
		expect(exposedRevoke).toBeUndefined();
	});

	it('requires disclosure before revoking and preserves the revoke CAS request', async () => {
		let truth: HostAccessLease = activeLease();
		const composer = addComposer();
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				truth = {
					...truth,
					state: 'revoked',
					revision: 6,
					expires_at: null,
					terminal_reason: 'Revoked by user.',
				};
				return response(truth);
			}
			return response({ leases: truth.state === 'active' ? [truth] : [] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());

		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(0);
		const revoke = await openRevokeButton();
		expect(revoke).toBeTruthy();
		expect(document.activeElement).not.toBe(revoke);
		revoke.click();

		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);
		});
		await vi.waitFor(() => expect(document.activeElement).toBe(composer));
		const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
		expect(post?.[0]).toBe('/api/host-access/leases/lease-1/revoke');
		expect(JSON.parse(String(post?.[1]?.body))).toEqual({
			revision: 5,
			application_hash: 'hash-1',
		});
	});

	it('restores focus to the composer when rejecting removes the pending trigger', async () => {
		let truth: HostAccessLease = waitingLease;
		const composer = addComposer();
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				truth = {
					...truth,
					state: 'rejected',
					revision: 5,
					terminal_reason: 'Rejected by user.',
				};
				return response(truth);
			}
			return response({ leases: truth.state === 'awaiting_user' ? [truth] : [] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		const reject = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject')!;
		reject.focus();
		reject.click();

		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeNull());
		expect(document.activeElement).toBe(composer);
	});

	it('does not steal focus when the user moves during an in-flight revoke', async () => {
		let resolveDecision!: (value: Response) => void;
		const decision = new Promise<Response>((resolve) => {
			resolveDecision = resolve;
		});
		let truth: HostAccessLease = activeLease();
		const composer = addComposer();
		const elsewhere = document.createElement('button');
		elsewhere.textContent = 'Elsewhere';
		host.appendChild(elsewhere);
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return decision;
			return response({ leases: [truth] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		const revoke = await openRevokeButton();
		revoke.focus();
		revoke.click();
		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);
		});

		elsewhere.focus();
		truth = activeLease({
			state: 'revoked',
			revision: 6,
			expires_at: null,
			terminal_reason: 'Revoked by user.',
		});
		resolveDecision(response(truth));

		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());
		expect(document.activeElement).toBe(elsewhere);
		expect(document.activeElement).not.toBe(composer);
	});

	it('restores focus when a later websocket refresh removes a revoked lease', async () => {
		let truth: HostAccessLease = activeLease();
		const composer = addComposer();
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return response(truth);
			return response({ leases: truth.state === 'active' ? [truth] : [] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		const revoke = await openRevokeButton();
		revoke.focus();
		revoke.click();

		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual([
				'GET',
				'POST',
				'GET',
			]);
		});
		expect(host.querySelector('.active-chip')).toBeTruthy();
		expect(document.activeElement).toBe(revoke);

		truth = activeLease({
			state: 'revoked',
			revision: 6,
			expires_at: null,
			terminal_reason: 'Revoked by user.',
		});
		emit('host_access_changed', { lease_id: truth.id });

		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());
		expect(document.activeElement).toBe(composer);
	});

	it('clears deferred focus restoration after a non-reconcilable revoke failure', async () => {
		let truth: HostAccessLease = activeLease();
		const composer = addComposer();
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				return response({ detail: 'Reviewer transport failed.' }, false, 503);
			}
			return response({ leases: [truth] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		const revoke = await openRevokeButton();
		revoke.focus();
		revoke.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Reviewer transport failed.'));

		truth = activeLease({
			state: 'revoked',
			revision: 6,
			expires_at: null,
			terminal_reason: 'Revoked elsewhere.',
		});
		emit('host_access_changed', { lease_id: truth.id });
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());

		expect(document.activeElement).not.toBe(composer);
	});

	it.each([
		[1_800, '30 minutes', true],
		[1_801, 'Invalid duration', false],
	])('validates a %i-second approval duration at the 30-minute boundary', async (
		seconds,
		expectedDuration,
		approvalAllowed,
	) => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({
			leases: [{ ...waitingLease, requested_duration_seconds: seconds }],
		})));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		expect(dialog.textContent).toContain(expectedDuration);
		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		expect(Boolean(approve)).toBe(approvalAllowed);
		expect(host.textContent?.includes('Approval is blocked.')).toBe(!approvalAllowed);
	});

	it('clears the disclosure on a thread switch and uses replacement truth', async () => {
		const threadOne = activeLease({
			id: 'lease-thread-1',
			purpose: 'Thread one purpose.',
		});
		const threadTwo = activeLease({
			id: 'lease-thread-2',
			root_thread_id: 'thread-2',
			root_thread_title: 'Other thread',
			purpose: 'Thread two purpose.',
			application_hash: 'hash-thread-2',
		});
		let resolveThreadTwo!: (value: Response) => void;
		const threadTwoResponse = new Promise<Response>((resolve) => {
			resolveThreadTwo = resolve;
		});
		let requestCount = 0;
		vi.stubGlobal('fetch', vi.fn(() => {
			requestCount += 1;
			return requestCount === 1
				? Promise.resolve(response({ leases: [threadOne] }))
				: threadTwoResponse;
		}));

		const switchable = createClassComponent({
			target: host,
			component: HostAccessLeaseControl,
			props: { threadId: 'thread-1' },
		});
		try {
			await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
			host.querySelector<HTMLElement>('.active-chip [aria-label="Host access"]')!.click();
			flushSync();
			await tick();
			expect(host.textContent).toContain('Thread one purpose.');

			switchable.$set({ threadId: 'thread-2' });
			flushSync();
			expect(host.querySelector('.active-chip')).toBeNull();
			expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
			expect(host.textContent).not.toContain('Thread one purpose.');

			resolveThreadTwo(response({ leases: [threadTwo] }));
			await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
			host.querySelector<HTMLElement>('.active-chip [aria-label="Host access"]')!.click();
			flushSync();
			await tick();
			expect(host.textContent).toContain('Thread two purpose.');
			expect(host.textContent).not.toContain('Thread one purpose.');
		} finally {
			switchable.$destroy();
		}
	});

	it('keeps the thread-local approval inline without stealing focus', async () => {
		const outside = document.createElement('button');
		outside.textContent = 'Outside control';
		document.body.appendChild(outside);
		outside.focus();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [waitingLease] }),
		));

		component = mountControl();
		flushSync();

		await vi.waitFor(() => {
			expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy();
		});
		expect(document.activeElement).toBe(outside);
		outside.remove();
	});

	it('renders the selected thread lease from scoped truth', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [waitingLease] }),
		));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => {
			expect(host.textContent).toContain('Home Assistant');
		});
	});

	it('fails closed when independent attestation is missing, but Reject stays actionable (CAS identity is intact)', async () => {
		const malformed = {
			...waitingLease,
			reviewer: { ...waitingLease.reviewer!, attestation: null },
			application_review_result: {
				...waitingLease.application_review_result,
				attestation: null,
			},
		};
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [malformed] }),
		));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		expect(dialog.textContent).toContain('Approval is blocked');
		expect(
			Array.from(dialog.querySelectorAll('button'))
				.some((button) => button.textContent?.trim() === 'Approve'),
			).toBe(false);
		// Missing attestation fails approvalReady but does not touch casReady's
		// fields (id/application_hash/decision_capability/revision), so Reject
		// is correctly still actionable — matching the pre-refactor base at
		// df33c562, where Reject was gated only by !casReady, independent of
		// approvalReady. Pinned here so a future change can't silently disable
		// it without the assertion catching it.
		const reject = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject');
		expect(reject?.disabled).toBe(false);
		});

	it.each([
		['purpose', { purpose: '' }, false],
		['duration', { requested_duration_seconds: '300' }, false],
		['lease identity', { id: '' }, true],
		['application hash', { application_hash: '' }, true],
		['decision capability', { decision_capability: '' }, true],
		['revision', { revision: 0 }, true],
		['creation time', { created_at: null }, false],
		['risk fields', { expected_resources: 42 }, false],
		['policy hash', { repo_policy_hash: null }, false],
		['project identity', { requesting_project_id: undefined }, false],
		['policy snapshot', { repo_policy_snapshot: null }, false],
		['policy exceptions', { requested_policy_exceptions: null }, false],
		['unknown policy exception', {
			requested_policy_exceptions: [{
				repo_name: 'corsia',
				policy: 'unknown',
				reason: 'Bypass',
			}],
		}, false],
	])('fails closed when awaiting-user %s data is malformed', async (_label, patch, rejectDisabled) => {
		const malformed = {
			...waitingLease,
			...patch,
		} as unknown as HostAccessLease;
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [malformed] }),
		));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		expect(dialog.textContent).toContain('Approval is blocked');
		expect(
			Array.from(dialog.querySelectorAll('button'))
				.some((button) => button.textContent?.trim() === 'Approve'),
			).toBe(false);
		// Reject is gated only by casReady (id/application_hash/
		// decision_capability/revision), independent of approvalReady's wider
		// data checks — matching the pre-refactor base. Only the four rows
		// above that mutate a casReady field expect Reject disabled=true; every
		// other malformation leaves the lease's CAS identity intact, so Reject
		// stays actionable even while Approve is correctly hidden.
		const reject = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject');
		expect(reject?.disabled).toBe(rejectDisabled);
	});

	it('ignores an awaiting-user lease without the selected thread identity', async () => {
		const unattributed = {
			...waitingLease,
			root_thread_id: '',
		};
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [unattributed] }),
		));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

		expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeNull();
		expect(host.textContent).not.toContain('Repair the media bridge.');
	});

	it('treats websocket frames as refresh hints and never as lease truth', async () => {
		const active = {
			...waitingLease,
			state: 'active' as const,
			revision: 5,
			expires_at: '2099-07-18T16:06:00+00:00',
		};
		const fetchMock = vi.fn().mockResolvedValue(response({ leases: [active] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());

		emit('host_access_changed', {
			lease: { ...waitingLease, purpose: 'UNTRUSTED WS PAYLOAD' },
		});

		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		expect(host.textContent).not.toContain('UNTRUSTED WS PAYLOAD');
	});

	it('refreshes authoritative truth on websocket reconnect', async () => {
		const fetchMock = vi.fn().mockResolvedValue(response({ leases: [waitingLease] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		emit('connected', {});

		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
	});

	it('keeps newer HTTP truth when an older request resolves last', async () => {
		let resolveOld!: (value: Response) => void;
		const oldRequest = new Promise<Response>((resolve) => {
			resolveOld = resolve;
		});
		const active = {
			...waitingLease,
			state: 'active' as const,
			revision: 5,
			expires_at: '2099-07-18T16:06:00+00:00',
		};
		const fetchMock = vi.fn()
			.mockReturnValueOnce(oldRequest)
			.mockResolvedValueOnce(response({ leases: [active] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		emit('host_access_changed', { lease_id: 'lease-1' });

		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		resolveOld(response({ leases: [waitingLease] }));
		await tick();
		await tick();

		expect(host.querySelector('.active-chip')).toBeTruthy();
		expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeNull();
	});

	it('does not display mutation response state before authoritative refresh', async () => {
		let resolveDecision!: (value: Response) => void;
		const decision = new Promise<Response>((resolve) => {
			resolveDecision = resolve;
		});
		let truth: HostAccessLease = waitingLease;
		const active = {
			...waitingLease,
			state: 'active' as const,
			revision: 5,
			expires_at: '2099-07-18T16:06:00+00:00',
		};
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return decision;
			return response({ leases: [truth] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!;
		approve.click();
		await tick();
		expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy();
		expect(host.querySelector('.active-chip')).toBeNull();

		truth = active;
		resolveDecision(response(active));
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
	});

	it.each([404, 409])('refreshes HTTP truth after a %s decision race', async (status) => {
		const active = {
			...waitingLease,
			state: 'active' as const,
			revision: 5,
			expires_at: '2099-07-18T16:06:00+00:00',
		};
		let getCount = 0;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				return response({ detail: 'Lease changed.' }, false, status);
			}
			getCount += 1;
			return response({ leases: [getCount === 1 ? waitingLease : active] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!;
		approve.click();

		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual([
			'GET',
			'POST',
			'GET',
		]);
	});

	it('disables approval when the selected thread truth becomes stale', async () => {
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Home Assistant'));

		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.textContent).toContain('State stale'));
		const dialog = await openPendingDrawer();
		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		expect(approve?.disabled).toBe(true);
	});

	it('takes the full mobile band for the stale notice, not the narrow chip slot', async () => {
		// The stale notice is prose and outranks the transitional row, so it is a
		// wide shape in its own right. `wide` is what ChatAttentionStack styles as
		// a full-band occupant; without it this text lands in a ~94px track.
		// The lease is active, not awaiting: the only other content is the compact
		// chip, so `wide` here can only be coming from the stale notice itself.
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [activeLease()] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());

		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.textContent).toContain('State stale'));

		expect(host.querySelector('.thread-host-access')!.classList).toContain('wide');
	});

	it('does not fetch or render without a selected thread', async () => {
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl(host, null);
		flushSync();
		await tick();

		expect(fetchMock).not.toHaveBeenCalled();
		expect(host.textContent).toBe('');
	});

	it('stays silent when the selected thread has no known lease and the initial refresh fails', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			response({ detail: 'offline' }, false, 503),
		);
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		await new Promise((resolve) => setTimeout(resolve, 0));
		await tick();

		expect(host.textContent).toBe('');
	});

	it('retries an unchanged expired revision with backoff until authoritative truth removes it', async () => {
		vi.useFakeTimers();
		const now = new Date('2026-07-18T16:00:00.000Z');
		vi.setSystemTime(now);
		const active = {
			...waitingLease,
			state: 'active' as const,
			revision: 5,
			expires_at: '2026-07-18T16:00:02.000Z',
		};
		let requestCount = 0;
		const fetchMock = vi.fn(async () => {
			requestCount += 1;
			return requestCount < 4
				? response({ leases: [active] })
				: response({
					leases: [{
						...active,
						state: 'expired',
						revision: 6,
						expires_at: null,
						terminal_reason: 'Lease expired.',
					}],
				});
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.advanceTimersByTimeAsync(0);
		await tick();
		expect(host.querySelector('.countdown')?.textContent).toBe('0:02');
		expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		expect(fetchMock).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(1000);
		await tick();
		expect(host.querySelector('.countdown')?.textContent).toBe('0:01');
		expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		expect(fetchMock).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(1000);
		await tick();
		expect(host.querySelector('.countdown')?.textContent).toBe('0:00');
		expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		expect(fetchMock).toHaveBeenCalledTimes(2);

		await vi.advanceTimersByTimeAsync(1_999);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(2);

		await vi.advanceTimersByTimeAsync(1);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(3);
		expect(host.querySelector('.countdown')?.textContent).toBe('0:00');

		await vi.advanceTimersByTimeAsync(3_999);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(3);

		await vi.advanceTimersByTimeAsync(1);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(4);
		expect(host.querySelector('.active-chip')).toBeNull();

		await vi.advanceTimersByTimeAsync(60_000);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(4);
	});

	it('continues expiry reconciliation at a capped interval while truth stays active', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-07-18T16:00:00.000Z'));
		const active = activeLease({
			expires_at: '2026-07-18T16:00:02.000Z',
		});
		const fetchMock = vi.fn().mockResolvedValue(response({ leases: [active] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.advanceTimersByTimeAsync(0);
		await tick();

		await vi.advanceTimersByTimeAsync(120_000);
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(7);
		expect(host.querySelector('.countdown')?.textContent).toBe('0:00');
	});

	it('does not leak another thread terminal failure into the selected thread', async () => {
		const blocked = {
			...waitingLease,
			root_thread_id: 'other-thread',
			root_thread_title: 'Other thread',
			state: 'blocked' as const,
			revision: 5,
			terminal_reason: 'application_reviewer_failure',
		};
		const active = {
			...waitingLease,
			id: 'active-lease',
			state: 'active' as const,
			revision: 5,
			expires_at: '2099-07-18T16:06:00+00:00',
		};
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
			response({ leases: [blocked, active] }),
		));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		expect(host.querySelector('[aria-label="Host access"]')?.querySelector('.host-access-icon')).toBeTruthy();
		expect(host.textContent).not.toContain('Home Assistant');
		expect(host.textContent).not.toContain('Other thread');
		expect(host.textContent).not.toContain('application_reviewer_failure');
		expect(host.textContent).not.toContain('System settings');
	});

	it.each<HostAccessLease['state']>(['blocked', 'expired', 'revoked', 'rejected'])(
		'does not surface selected-thread %s history',
		async (state) => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({
				leases: [{
					...waitingLease,
					state,
					revision: 5,
					terminal_reason: `${state}_reason`,
				}],
			})));

			component = mountControl();
			flushSync();
			await tick();
			await new Promise((resolve) => setTimeout(resolve, 0));

			expect(host.textContent).toBe('');
		},
	);

	it('keeps an in-flight decision locked across component remounts', async () => {
		let resolveDecision!: (value: Response) => void;
		const decision = new Promise<Response>((resolve) => {
			resolveDecision = resolve;
		});
		const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'POST'
				? decision
				: Promise.resolve(response({ leases: [waitingLease] }))
		));
		vi.stubGlobal('fetch', fetchMock);
		const button = (label: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((candidate) => candidate.textContent?.trim() === label);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		await openPendingDrawer();
		await vi.waitFor(() => expect(button('Approve')).toBeTruthy());
		button('Approve')!.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);

		unmount(component);
		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		await openPendingDrawer();
		await vi.waitFor(() => expect(button('Approve')).toBeTruthy());
		expect(button('Approve')?.disabled).toBe(true);
		expect(button('Reject')?.disabled).toBe(true);
		button('Reject')!.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);

		resolveDecision(response(waitingLease));
		await vi.waitFor(() => expect(button('Approve')?.disabled).toBe(false));
	});

	it('disables the pending decision controls while a different lease has a decision in flight', async () => {
		let resolveRevoke!: (value: Response) => void;
		const revoke = new Promise<Response>((resolve) => {
			resolveRevoke = resolve;
		});
		const otherActive = activeLease({ id: 'lease-2' });
		const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'POST'
				? revoke
				: Promise.resolve(response({ leases: [waitingLease, otherActive] }))
		));
		vi.stubGlobal('fetch', fetchMock);
		const button = (label: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((candidate) => candidate.textContent?.trim() === label);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip [aria-label="Host access"]')).toBeTruthy());
		const revokeButton = await openRevokeButton();
		revokeButton.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);

		host.querySelector('[role="dialog"]')!
			.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));

		await openPendingDrawer();
		await vi.waitFor(() => expect(button('Reject')).toBeTruthy());
		expect(button('Reject')?.disabled).toBe(true);
		expect(button('Approve')?.disabled).toBe(true);
		button('Reject')!.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);

		resolveRevoke(response(otherActive));
		await vi.waitFor(() => expect(button('Reject')?.disabled).toBe(false));
	});

	it('disables Reject, not only Approve, when the selected thread truth becomes stale', async () => {
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Home Assistant'));

		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.textContent).toContain('State stale'));
		const dialog = await openPendingDrawer();
		const reject = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject');
		expect(reject?.disabled).toBe(true);
		reject!.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(0);
	});

	it('disables Reject and no-ops its click when CAS identity is malformed', async () => {
		const malformed = { ...waitingLease, id: '' } as unknown as HostAccessLease;
		const fetchMock = vi.fn().mockResolvedValue(response({ leases: [malformed] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		const reject = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject');

		expect(reject?.disabled).toBe(true);
		reject!.click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(0);
	});

	it('claims a decision only once when the same lease is clicked twice before state settles', async () => {
		let resolveDecision!: (value: Response) => void;
		const decision = new Promise<Response>((resolve) => {
			resolveDecision = resolve;
		});
		const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'POST'
				? decision
				: Promise.resolve(response({ leases: [waitingLease] }))
		));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		const reject = () => Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject')!;

		reject().click();
		reject().click();
		await tick();
		expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);

		resolveDecision(response({ ...waitingLease, state: 'rejected' }));
		await tick();
	});

	it('aborts the active GET and removes refresh listeners during cleanup', async () => {
		let requestSignal: AbortSignal | undefined;
		const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
			requestSignal = init?.signal ?? undefined;
			return new Promise<Response>(() => {});
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		expect(fetchMock).toHaveBeenCalledTimes(1);

		unmount(component);
		component = null;
		expect(requestSignal?.aborted).toBe(true);
		emit('host_access_changed', { lease_id: 'lease-1' });
		emit('connected', {});
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('keeps a non-conflict mutation failure visible and disables another decision', async () => {
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockResolvedValueOnce(response(
				{ detail: 'Reviewer transport failed.' },
				false,
				503,
			));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();

		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!;
		approve.click();

		await vi.waitFor(() => {
			expect(host.textContent).toContain('Reviewer transport failed.');
		});
		expect(approve.disabled).toBe(true);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});
});

describe('HostAccessLeaseControl — AdaptiveDrawer routing (#1123)', () => {
	it('does not auto-open the pending request, then opens exactly one AdaptiveDrawer dialog on tap', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ leases: [waitingLease] })));

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());

		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
		expect(host.textContent).not.toContain('Repair the media bridge.');

		const trigger = host.querySelector<HTMLButtonElement>('[aria-label="Review temporary host access"]')!;
		expect(trigger.tagName).toBe('BUTTON');
		trigger.click();
		flushSync();
		await tick();

		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(1);
		const dialog = host.querySelector('[role="dialog"]');
		expect(dialog?.getAttribute('aria-label')).toBe('Review temporary host access');
		expect(dialog?.textContent).toContain('Repair the media bridge.');
		const reject = Array.from(dialog!.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject');
		expect(reject).toBeTruthy();

		const done = host.querySelector('.drawer-done') as HTMLButtonElement;
		done.click();
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));
	});

	it('opens the active lease detail in exactly one AdaptiveDrawer dialog, keeps the CAS revoke payload, and returns focus to the trigger', async () => {
		let truth: HostAccessLease = activeLease();
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				truth = {
					...truth,
					state: 'revoked',
					revision: 6,
					expires_at: null,
					terminal_reason: 'Revoked by user.',
				};
				return response(truth);
			}
			return response({ leases: [truth] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);

		const trigger = host.querySelector<HTMLButtonElement>('.active-chip [aria-label="Host access"]')!;
		trigger.click();
		flushSync();
		await tick();

		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(1);
		const dialog = host.querySelector('[role="dialog"]')!;
		expect(dialog.textContent).toContain('Repair the media bridge.');

		dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));
		expect(document.activeElement).toBe(trigger);

		trigger.click();
		flushSync();
		await tick();
		const revoke = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Revoke host access')!;
		revoke.click();

		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);
		});
		const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
		expect(post?.[0]).toBe('/api/host-access/leases/lease-1/revoke');
		expect(JSON.parse(String(post?.[1]?.body))).toEqual({
			revision: 5,
			application_hash: 'hash-1',
		});
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());
		expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
	});

	it('closes the pending drawer from Done, Escape, and the backdrop alike', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ leases: [waitingLease] })));
		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const trigger = host.querySelector<HTMLButtonElement>('[aria-label="Review temporary host access"]')!;

		trigger.click();
		flushSync();
		await tick();
		const scrim = host.querySelector('[data-adaptive-drawer]') as HTMLElement;
		scrim.dispatchEvent(new Event('pointerdown', { bubbles: true }));
		scrim.click();
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));

		trigger.click();
		flushSync();
		await tick();
		host.querySelector('[role="dialog"]')!
			.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));
		expect(document.activeElement).toBe(trigger);
	});
});
