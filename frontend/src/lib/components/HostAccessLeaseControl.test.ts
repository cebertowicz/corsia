import { flushSync, mount, tick, unmount } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HostAccessLeaseControl from './HostAccessLeaseControl.svelte';
import { claimDecision, isCurrentDecision, releaseDecision } from './hostAccessDecisionAttempts';
import { token } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';
import type { HostAccessFill, HostAccessItem, HostAccessLease, HostAccessScript } from '$lib/api/hostAccess';

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
	return host.querySelector<HTMLElement>('[role="dialog"]')!;
}

/** The active chip by its accessible name, the kind: "Host access" or "Saved login". */
function chipTrigger(kind: 'Host access' | 'Saved login'): HTMLButtonElement | null {
	return host.querySelector<HTMLButtonElement>(`.active-chip [aria-label="${kind}"]`);
}

async function openActiveDrawer(): Promise<HTMLElement> {
	const trigger = chipTrigger('Host access')!;
	trigger.click();
	flushSync();
	await tick();
	return host.querySelector<HTMLElement>('[role="dialog"]')!;
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
	it('keeps one decision in flight per lease', () => {
		const attempt = claimDecision('helper-test')!;
		expect(claimDecision('helper-test')).toBeNull();
		expect(isCurrentDecision('helper-test', attempt)).toBe(true);
		releaseDecision('helper-test', attempt);
		expect(isCurrentDecision('helper-test', attempt)).toBe(false);
	});
	it.each([
		['activation_reviewer_unavailable', 'Host access couldn’t start because its reviewer model can’t run. Choose another reviewer model in Settings › System, then ask the agent to request access again.'],
		['activation_failed', 'Host access couldn’t start. Ask the agent to request access again.'],
		['raw secret detail', 'Host access couldn’t start. Ask the agent to request access again.'],
	])('shows a safe message when approval returns blocked: %s', async (reason, message) => {
		let decided = false;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				decided = true;
				return response({ ...waitingLease, state: 'blocked', terminal_reason: reason });
			}
			return response({ leases: decided ? [] : [waitingLease] });
		}));
		component = mountControl();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain(message));
		expect(host.textContent).not.toContain('raw secret detail');
		emit('host_access_changed', {});
		await tick();
		expect(host.textContent).toContain(message);
	});

	it.each(['active', 'awaiting_user', 'reviewing', 'activating'] as const)(
		'clears the failure when a new %s lease appears, but not for the failed lease', async (state) => {
			const message = 'Host access couldn’t start. Ask the agent to request access again.';
			let listed: HostAccessLease[] = [waitingLease];
			const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
				if (init?.method === 'POST') {
					listed = [];
					return response({ ...waitingLease, state: 'blocked', terminal_reason: 'activation_failed' });
				}
				return response({ leases: listed });
			});
			vi.stubGlobal('fetch', fetchMock);
			component = mountControl();
			await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
			const dialog = await openPendingDrawer();
			Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
				.find((button) => button.textContent?.trim() === 'Approve')!.click();
			await vi.waitFor(() => expect(host.textContent).toContain(message));
			await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3));
			listed = [{ ...waitingLease, state }];
			emit('host_access_changed', {});
			await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(4));
			await tick();
			expect(host.textContent).toContain(message);
			listed = [{ ...waitingLease, id: 'lease-2', state }];
			emit('host_access_changed', {});
			await vi.waitFor(() => expect(host.textContent).not.toContain(message));
		},
	);

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
		expect(dialog.textContent).toContain('Beyond the usual rules');
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

	it('approves a saved-login fill for the picked duration, then revokes it from its chip', async () => {
		const waitingFill: HostAccessFill = {
			id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
			agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
			revision: 1, application_hash: 'fill-hash', decision_capability: 'fill-decision',
			max_duration_seconds: 1800, requested_duration_seconds: null, duration_seconds: null, standing: false, expires_at: null,
			created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
		};
		let state: HostAccessFill | null = waitingFill;
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				state = String(input).endsWith('/approve')
					? {
						...waitingFill, state: 'active', revision: 2, duration_seconds: 1800,
						expires_at: '2099-07-18T16:30:00Z', decision_capability: 'fill-revoke',
					}
					: null;
				return response({});
			}
			return response({ leases: state ? [state] : [] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		const trigger = await vi.waitFor(() => {
			const found = host.querySelector<HTMLButtonElement>('[aria-label="Review a request to sign in with a saved login"]');
			expect(found).toBeTruthy();
			return found!;
		});
		trigger.click();
		flushSync();
		await tick();
		const dialog = host.querySelector('[role="dialog"]')!;
		expect(dialog.textContent).toContain('Bookkeeper');
		expect(dialog.textContent).toContain('me@example.com');
		dialog.querySelector<HTMLButtonElement>('[data-seconds="1800"]')!.click();
		flushSync();
		Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!.click();
		await tick();

		await vi.waitFor(() => expect(chipTrigger('Saved login')).toBeTruthy());
		const approve = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
		expect(approve?.[0]).toBe('/api/host-access/leases/fills/fill-1/approve');
		expect(JSON.parse(String(approve?.[1]?.body))).toEqual({
			revision: 1, application_hash: 'fill-hash', duration_seconds: 1800,
		});

		chipTrigger('Saved login')!.click();
		flushSync();
		await tick();
		const activeDialog = host.querySelector('[role="dialog"]')!;
		// The pending sheet's head (kind label over the agent's name) and its section-label rows.
		expect(activeDialog.querySelector('.decision-head .head-label')?.textContent).toBe('Sign in with a saved login');
		expect(activeDialog.querySelector('.decision-head h2')?.textContent).toBe('Bookkeeper');
		expect([...activeDialog.querySelectorAll('dt.section-label')].map((label) => label.textContent))
			.toEqual(['Site', 'Username', 'For how long', 'Time remaining']);
		expect([...activeDialog.querySelectorAll('dd')].slice(0, 2).map((value) => value.textContent))
			.toEqual(['https://example.com', 'me@example.com']);
		Array.from(activeDialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Revoke approval')!.click();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());
		const revoke = fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')[1];
		expect(revoke?.[0]).toBe('/api/host-access/leases/fills/fill-1/revoke');
		expect(JSON.parse(String(revoke?.[1]?.body))).toEqual({ revision: 2, application_hash: 'fill-hash' });
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
		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t revoke the approval. Try again.'));
		expect(host.textContent).not.toContain('Reviewer transport failed.');

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
		// Approve is always there; it is off when the request can't be approved.
		expect(approve?.disabled).toBe(!approvalAllowed);
		expect(Boolean(dialog.querySelector('.blocked-line'))).toBe(!approvalAllowed);
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
			chipTrigger('Host access')!.click();
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
			chipTrigger('Host access')!.click();
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

		expect(dialog.querySelector('.blocked-line')?.textContent?.trim()).toBe('This request is missing details CORSIA needs to check it, so you can’t approve it. Reject it and the agent can ask again.');
		// Approve is shown disabled, never hidden; reading again won't change the request, so no Retry.
		expect(
			Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
				.find((button) => button.textContent?.trim() === 'Approve')?.disabled,
			).toBe(true);
		expect(Array.from(dialog.querySelectorAll('button')).some((button) => button.textContent?.trim() === 'Retry')).toBe(false);
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

		// Without its decision fields Reject can't be sent either, so the line says the request stays.
		expect(dialog.querySelector('.blocked-line')?.textContent?.trim()).toBe(rejectDisabled
			? 'This request is missing details CORSIA needs to check it, so you can’t approve or reject it. It stays here until the agent withdraws it or CORSIA restarts.'
			: 'This request is missing details CORSIA needs to check it, so you can’t approve it. Reject it and the agent can ask again.');
		// Approve is shown disabled, never hidden; reading again won't change the request, so no Retry.
		expect(
			Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
				.find((button) => button.textContent?.trim() === 'Approve')?.disabled,
			).toBe(true);
		expect(Array.from(dialog.querySelectorAll('button')).some((button) => button.textContent?.trim() === 'Retry')).toBe(false);
		// Reject is gated only by casReady (id/application_hash/
		// decision_capability/revision), independent of approvalReady's wider
		// data checks — matching the pre-refactor base. Only the four rows
		// above that mutate a casReady field expect Reject disabled=true; every
		// other malformation leaves the lease's CAS identity intact, so Reject
		// stays actionable while Approve is shown disabled.
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

	it('says a refused stale Reject found new details, never the server line, over the refreshed request', async () => {
		const changed = { ...waitingLease, revision: 5, application_hash: 'hash-2', purpose: 'Restart the Home Assistant add-on' };
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503))
			.mockResolvedValueOnce(response({ detail: 'Lease changed.' }, false, 409))
			.mockResolvedValue(response({ leases: [changed] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Home Assistant'));
		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t check'));
		const dialog = await openPendingDrawer();
		Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Reject')!.click();

		// The re-read brings the changed request back, and the drawer still says why the Reject didn't land.
		await vi.waitFor(() => expect(dialog.textContent).toContain('Restart the Home Assistant add-on'));
		const notice = dialog.querySelector('.decision-actions .decision-notice');
		expect(notice?.textContent).toBe('This request has new details. Review them, then approve or reject it.');
		expect(dialog.textContent).not.toContain('Lease changed.');
		expect(notice?.getAttribute('role')).toBe('alert');
		expect(dialog.querySelector('.decision-head .status')?.textContent?.trim()).toBe('Needs approval');
		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		expect(approve?.disabled).toBe(false);
	});

	it.each([
		['a 500', () => response({ detail: 'Host access could not start. Try again in a moment.' }, false, 500)],
		['no connection', () => Promise.reject(new TypeError('Failed to fetch'))],
	])('says Approve failed after %s, keeps Approve on, and drops the line on the next decision', async (_label, failure) => {
		let failApprove = true;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST' && failApprove) return failure();
			if (init?.method === 'POST') return response({ ...waitingLease, state: 'rejected' });
			return response({ leases: [waitingLease] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		const button = (name: string) => Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((candidate) => candidate.textContent?.trim() === name)!;
		button('Approve').click();
		await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')?.textContent)
			.toBe('Couldn’t approve the request. Try again.'));
		// Never the server's own words, and the person can try again straight away.
		expect(dialog.textContent).not.toContain('Host access could not start');
		expect(dialog.querySelector('.decision-head .status')?.textContent?.trim()).toBe('Needs approval');
		expect(button('Approve').disabled).toBe(false);
		expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual(['GET', 'POST']);

		failApprove = false;
		button('Reject').click();
		await vi.waitFor(() => expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(2));
		expect(dialog.querySelector('.decision-notice')).toBeNull();
	});

	it('says an Approve refused for new details reads the request again and shows it', async () => {
		const changed = { ...waitingLease, revision: 5, application_hash: 'hash-2', purpose: 'Restart the Home Assistant add-on' };
		let getCount = 0;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return response({ detail: 'Lease changed.' }, false, 409);
			getCount += 1;
			return response({ leases: [getCount === 1 ? waitingLease : changed] });
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		const dialog = await openPendingDrawer();
		Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve')!.click();

		await vi.waitFor(() => expect(dialog.textContent).toContain('Restart the Home Assistant add-on'));
		expect(dialog.querySelector('.decision-notice')?.textContent)
			.toBe('This request has new details. Review them, then approve or reject it.');
		expect(dialog.textContent).not.toContain('Lease changed.');
		expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual(['GET', 'POST', 'GET']);
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
		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t check'));
		const dialog = await openPendingDrawer();
		const approve = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		expect(approve?.disabled).toBe(true);
		// The drawer's status is coloured text, not a pill, and its Retry is a text action.
		const status = dialog.querySelector('.decision-head .status');
		expect(status?.textContent?.trim()).toBe('Couldn’t check');
		expect(status?.classList.contains('stale')).toBe(true);
		expect(dialog.querySelector('.pill')).toBeNull();
		const retry = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Retry');
		expect(retry?.className).toContain('ghost');
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
		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t check host access.'));

		expect(host.querySelector('.thread-host-access')!.classList).toContain('wide');
		// With only a countdown on screen the row is the sentence and Retry: no pill, no server words.
		const row = host.querySelector('.status-row.stale')!;
		expect(row.querySelector('.dotmark')).toBeNull();
		expect(row.textContent?.replace(/\s+/g, ' ').trim()).toBe('Couldn’t check host access. Retry');
		expect(host.textContent).not.toContain('offline');
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
		expect(fetchMock).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(1000);
		await tick();
		expect(host.querySelector('.countdown')?.textContent).toBe('0:01');
		expect(fetchMock).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(1000);
		await tick();
		expect(host.querySelector('.countdown')?.textContent).toBe('0:00');
		expect(chipTrigger('Host access')).toBeTruthy();
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
		expect(chipTrigger('Host access')).toBeTruthy();
		expect(chipTrigger('Host access')?.querySelector('.host-access-icon')).toBeTruthy();
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
		await vi.waitFor(() => expect(chipTrigger('Host access')).toBeTruthy());
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

	it('keeps Reject enabled when the selected thread truth becomes stale, and sends the last view', async () => {
		// The server checks the revision and hash it is sent, so a Reject from the last view either ends
		// that same request or comes back refused as changed; Approve stays disabled.
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503))
			.mockResolvedValueOnce(response({ ...waitingLease, state: 'rejected' }))
			.mockResolvedValue(response({ leases: [] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Home Assistant'));

		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t check'));
		const dialog = await openPendingDrawer();
		const buttons = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'));
		expect(buttons.find((button) => button.textContent?.trim() === 'Approve')?.disabled).toBe(true);
		const reject = buttons.find((button) => button.textContent?.trim() === 'Reject');
		expect(reject?.disabled).toBe(false);
		reject!.click();
		await vi.waitFor(() => expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1));
		const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')!;
		expect(post[0]).toBe('/api/host-access/leases/lease-1/reject');
		expect(JSON.parse(String(post[1]?.body))).toEqual({ revision: 4, application_hash: 'hash-1' });
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeNull());
	});

	it('keeps a stale saved-login fill rejectable through its own route', async () => {
		const waitingFill: HostAccessFill = {
			id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
			agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
			revision: 1, application_hash: 'fill-hash', decision_capability: 'fill-decision',
			max_duration_seconds: 1800, requested_duration_seconds: null, duration_seconds: null, standing: false, expires_at: null,
			created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
		};
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingFill] }))
			.mockResolvedValueOnce(response({ detail: 'offline' }, false, 503))
			.mockResolvedValueOnce(response({ ...waitingFill, state: 'denied' }))
			.mockResolvedValue(response({ leases: [] }));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		const trigger = await vi.waitFor(() => {
			const found = host.querySelector<HTMLButtonElement>('[aria-label="Review a request to sign in with a saved login"]');
			expect(found).toBeTruthy();
			return found!;
		});
		emit('host_access_changed', { lease_id: 'fill-1' });
		await vi.waitFor(() => expect(trigger.textContent).toContain('Couldn’t check'));
		trigger.click();
		flushSync();
		await tick();
		const dialog = host.querySelector('[role="dialog"]')!;
		const buttons = Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'));
		expect(buttons.find((button) => button.textContent?.trim() === 'Approve')?.disabled).toBe(true);
		const reject = buttons.find((button) => button.textContent?.trim() === 'Reject')!;
		expect(reject.disabled).toBe(false);
		reject.click();
		await vi.waitFor(() => expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1));
		const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')!;
		expect(post[0]).toBe('/api/host-access/leases/fills/fill-1/reject');
		expect(JSON.parse(String(post[1]?.body))).toEqual({ revision: 1, application_hash: 'fill-hash' });
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

	const waitingScript: HostAccessScript = {
		id: 'script-1', kind: 'script', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
		application_hash: 'script-hash', decision_capability: 'script-decision',
		script_name: 'task.py', script_path: '/workspace/task.py', trigger: { cadence_seconds: 600 },
		purpose: 'Check the result', user_request: 'Please check the result', reviewer_reason: 'Matches the request',
		created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
	};
	const SCRIPT_TRIGGER = 'Review permission for this script to act as your agent and use the agents’ GitHub account';
	const press = (dialog: Element, name: string) => Array.from(dialog.querySelectorAll<HTMLButtonElement>('button'))
		.find((button) => button.textContent?.trim() === name)!;
	const FILL_TRIGGER = 'Review a request to sign in with a saved login';
	const fillRequest: HostAccessFill = {
		id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
		agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
		revision: 1, application_hash: 'fill-hash', decision_capability: 'fill-decision',
		max_duration_seconds: 1800, requested_duration_seconds: null, duration_seconds: null, standing: false, expires_at: null,
		created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
	};
	async function openDrawerFor(item: HostAccessItem): Promise<HTMLElement> {
		if (item.state === 'active') return openActiveDrawer();
		const label = item.kind === 'script' ? SCRIPT_TRIGGER : item.kind === 'fill' ? FILL_TRIGGER : 'Review temporary host access';
		await vi.waitFor(() => expect(host.querySelector(`[aria-label="${label}"]`)).toBeTruthy());
		host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click();
		flushSync();
		await tick();
		return host.querySelector<HTMLElement>('[role="dialog"]')!;
	}

	const presses = [
		['Approve', waitingLease, 'Couldn’t approve the request. Try again.'],
		['Reject', waitingLease, 'Couldn’t reject the request. Try again.'],
		['Deny', waitingScript, 'Couldn’t deny the request. Try again.'],
		['Revoke host access', activeLease(), 'Couldn’t revoke the approval. Try again.'],
	] as const;
	const failures = [
		['a 500', 'Internal Server Error', () => response({ detail: 'Internal Server Error' }, false, 500)],
		['a 403', 'A current interactive user decision is required',
			() => response({ detail: 'A current interactive user decision is required' }, false, 403)],
		['no connection', 'Failed to fetch', () => Promise.reject(new TypeError('Failed to fetch'))],
	] as const;
	for (const [button, item, line] of presses) {
		it.each(failures)(`shows one line, not the could-not-check line, when ${button} fails with %s, and keeps it on`, async (
			_label, serverText, failure,
		) => {
			const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
				init?.method === 'POST' ? failure() : response({ leases: [item] })));
			vi.stubGlobal('fetch', fetchMock);
			component = mountControl();
			flushSync();
			if (item.state === 'active') await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
			const dialog = await openDrawerFor(item);
			press(dialog, button).click();

			await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')?.textContent).toBe(line));
			// One line for the press: no could-not-check line in the drawer or the strip, and no server words.
			expect([...dialog.querySelectorAll('.blocked-line')].map((found) => found.textContent?.trim())).toEqual([line]);
			expect(host.textContent).not.toContain('Couldn’t check');
			expect(host.textContent).not.toContain(serverText);
			expect(press(dialog, button).disabled).toBe(false);
			expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual(['GET', 'POST']);
		});
	}

	it('says a script request has new details in its own words when Deny is refused (409)', async () => {
		let getCount = 0;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return response({ detail: 'Script request changed.' }, false, 409);
			getCount += 1;
			return response({ leases: [getCount === 1 ? waitingScript : { ...waitingScript, application_hash: 'script-hash-2' }] });
		}));
		component = mountControl();
		flushSync();
		const dialog = await openDrawerFor(waitingScript);
		press(dialog, 'Deny').click();

		await vi.waitFor(() => expect(getCount).toBe(2));
		await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')?.textContent)
			.toBe('This request has new details. Review them, then approve or deny it.'));
		expect(dialog.textContent).not.toContain('Script request changed.');
	});

	it('says an approval changed when Revoke is refused (409), and drops the line when it leaves the list', async () => {
		let truth: HostAccessLease[] = [activeLease()];
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				truth = [activeLease({ revision: 6 })];
				return response({ detail: 'Lease changed.' }, false, 409);
			}
			return response({ leases: truth });
		}));
		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		const dialog = await openActiveDrawer();
		press(dialog, 'Revoke host access').click();

		await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')?.textContent)
			.toBe('This approval has changed. Check it, then revoke it if you still need to.'));
		expect(host.textContent).not.toContain('Lease changed.');
		expect(press(dialog, 'Revoke host access').disabled).toBe(false);

		// The approval ends elsewhere and leaves the list: the line goes with it.
		truth = [];
		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeNull());
		expect(host.textContent).not.toContain('This approval has changed');
		expect(host.querySelector('.decision-notice')).toBeNull();
	});

	it('keeps a gone request on its path: a refused fill Reject (404) reads the list again without it', async () => {
		const waitingFill: HostAccessFill = {
			id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
			agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
			revision: 1, application_hash: 'fill-hash', decision_capability: 'fill-decision',
			max_duration_seconds: 1800, requested_duration_seconds: null, duration_seconds: null, standing: false, expires_at: null,
			created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
		};
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingFill] }))
			.mockResolvedValueOnce(response({
				detail: 'This sign-in request is closed. Ask the agent to try again if it still needs to sign in.',
			}, false, 404))
			.mockResolvedValue(response({ leases: [] }));
		vi.stubGlobal('fetch', fetchMock);
		component = mountControl();
		flushSync();
		const label = 'Review a request to sign in with a saved login';
		await vi.waitFor(() => expect(host.querySelector(`[aria-label="${label}"]`)).toBeTruthy());
		host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click();
		flushSync();
		await tick();
		press(host.querySelector('[role="dialog"]')!, 'Reject').click();

		await vi.waitFor(() => expect(host.querySelector(`[aria-label="${label}"]`)).toBeNull());
		expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? 'GET')).toEqual(['GET', 'POST', 'GET']);
		expect(host.textContent).not.toContain('Couldn’t reject');
		expect(host.querySelector('.decision-notice')).toBeNull();
	});

	/** A browser moves focus to <body> when the pressed button turns disabled for its request; jsdom doesn't. */
	function pressAsBrowser(button: HTMLButtonElement): void {
		button.focus();
		button.click();
		button.blur();
	}
	async function escapeClosesDrawer(): Promise<void> {
		document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[role="dialog"]')).toHaveLength(0));
	}
	async function mountWith(item: HostAccessItem, fetchMock: ReturnType<typeof vi.fn>): Promise<HTMLElement> {
		vi.stubGlobal('fetch', fetchMock);
		component = mountControl();
		flushSync();
		if (item.state === 'active') await vi.waitFor(() => expect(host.querySelector('.active-chip')).toBeTruthy());
		return openDrawerFor(item);
	}

	const refocusCases = [
		['a saved login’s', 'Reject', 500, fillRequest],
		['a saved login’s', 'Reject', 409, fillRequest],
		['a host-access request’s', 'Reject', 500, waitingLease],
		['a script request’s', 'Deny', 409, waitingScript],
		['an active approval’s', 'Revoke host access', 500, activeLease()],
	] as const;
	it.each(refocusCases)('puts focus back on %s %s after a %i, so Escape still closes the drawer', async (
		_kind, button, status, item,
	) => {
		const dialog = await mountWith(item, vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'POST' ? response({ detail: 'Server words.' }, false, status) : response({ leases: [item] }))));
		const pressed = press(dialog, button);
		pressAsBrowser(pressed);
		expect(document.activeElement).toBe(document.body);

		await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')).toBeTruthy());
		await vi.waitFor(() => expect(document.activeElement).toBe(pressed));
		expect(pressed.disabled).toBe(false);
		await escapeClosesDrawer();
	});

	it('puts focus on the drawer panel when the pressed button is still off after a failed press', async () => {
		// Approve is refused for new details, and the request read again is missing what CORSIA needs: Approve stays off.
		const incomplete = {
			...waitingLease, revision: 5,
			reviewer: { ...waitingLease.reviewer!, attestation: null },
			application_review_result: { ...waitingLease.application_review_result, attestation: null },
		};
		let getCount = 0;
		const dialog = await mountWith(waitingLease, vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return response({ detail: 'Lease changed.' }, false, 409);
			getCount += 1;
			return response({ leases: [getCount === 1 ? waitingLease : incomplete] });
		}));
		const approve = press(dialog, 'Approve');
		pressAsBrowser(approve);

		await vi.waitFor(() => expect(getCount).toBe(2));
		await vi.waitFor(() => expect(document.activeElement).toBe(dialog));
		expect(press(dialog, 'Approve').disabled).toBe(true);
		await escapeClosesDrawer();
	});

	it('leaves focus where the person moved it while a decision that then fails was in flight', async () => {
		let fail!: () => void;
		const held = new Promise<Response>((resolve) => {
			fail = () => resolve(response({ detail: 'Internal Server Error' }, false, 500));
		});
		const dialog = await mountWith(waitingLease, vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'POST' ? held : response({ leases: [waitingLease] }))));
		pressAsBrowser(press(dialog, 'Reject'));
		const done = press(dialog, 'Done');
		done.focus();
		fail();

		await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')).toBeTruthy());
		await tick();
		expect(document.activeElement).toBe(done);
	});

	const noBody = () => ({ ok: false, status: 404, json: async () => { throw new SyntaxError('no body'); } }) as unknown as Response;
	const stillListedCases = [
		['Reject', 'a host-access request', waitingLease, 'Host-access lease not found',
			'This request is closed, so there’s nothing to reject.'],
		['Reject', 'a saved login', fillRequest,
			'This sign-in request is closed. Ask the agent to try again if it still needs to sign in.',
			'This request is closed, so there’s nothing to reject.'],
		['Deny', 'a script request', waitingScript, 'Script request not found',
			'This request is closed, so there’s nothing to deny.'],
		['Revoke host access', 'an active approval', activeLease(), 'Host-access lease not found',
			'This approval has ended, so there’s nothing to revoke.'],
	] as const;
	for (const [button, kind, item, detail, line] of stillListedCases) {
		it.each([['its detail', detail], ['no body', 'Host access request failed (404)']])(
			`shows one line of its own when ${button} on ${kind} finds it gone (404, %s) while it is still listed`,
			async (_label, serverText) => {
				const dialog = await mountWith(item, vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
					if (init?.method !== 'POST') return response({ leases: [item] });
					return serverText === detail ? response({ detail }, false, 404) : noBody();
				}));
				const pressed = press(dialog, button);
				pressAsBrowser(pressed);

				await vi.waitFor(() => expect(dialog.querySelector('.decision-notice')?.textContent).toBe(line));
				expect([...dialog.querySelectorAll('.blocked-line')].map((found) => found.textContent?.trim())).toEqual([line]);
				expect(host.textContent).not.toContain(serverText);
				expect(host.textContent).not.toContain('Couldn’t check');
				await vi.waitFor(() => expect(document.activeElement).toBe(pressed));
			},
		);
	}

	const goneCases = [
		['Approve', 'a saved login', fillRequest], ['Approve', 'a host-access request', waitingLease],
		['Reject', 'a host-access request', waitingLease], ['Reject', 'a saved login', fillRequest],
		['Deny', 'a script request', waitingScript], ['Revoke host access', 'an active approval', activeLease()],
	] as const;
	it.each(goneCases)('sends focus to the composer when %s on %s finds it gone (404) and its drawer closes', async (
		button, _kind, item,
	) => {
		const composer = addComposer();
		let gone = false;
		const dialog = await mountWith(item, vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method !== 'POST') return response({ leases: gone ? [] : [item] });
			gone = true;
			return response({ detail: 'Not found' }, false, 404);
		}));
		pressAsBrowser(press(dialog, button));

		await vi.waitFor(() => expect(host.querySelectorAll('[role="dialog"]')).toHaveLength(0));
		await vi.waitFor(() => expect(document.activeElement).toBe(composer));
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

		// No chrome band above the sheet's own head: its title row is the top row, a drag handle, with Done on it.
		expect(dialog!.querySelector('.drawer-head')).toBeNull();
		const head = dialog!.querySelector('.decision-head')!;
		expect(head.hasAttribute('data-drawer-handle')).toBe(true);
		expect(head.querySelector('h2')?.textContent).toBe('Home Assistant');
		const done = head.querySelector<HTMLButtonElement>('button.head-done')!;
		expect(done.textContent?.trim()).toBe('Done');
		done.click();
		flushSync();
		await vi.waitFor(() => expect(host.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0));
		expect(document.activeElement).toBe(trigger);
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

		const trigger = chipTrigger('Host access')!;
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

describe('HostAccessLeaseControl — fixed sentences, never raw errors', () => {
	const STALE = 'Couldn’t check host access.';
	const labelCount = (label: string) => Array.from(document.body.querySelectorAll('*'))
		.filter((element) => element.children.length === 0 && element.textContent?.trim() === label).length;

	it('shows the fixed sentence when the re-read fails, logs the raw error, and labels the request once', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(response({ leases: [waitingLease] }))
			.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		vi.stubGlobal('fetch', fetchMock);

		component = mountControl();
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Home Assistant'));
		emit('host_access_changed', { lease_id: 'lease-1' });
		await vi.waitFor(() => expect(document.body.textContent).toContain(STALE));

		expect(document.body.textContent).not.toContain('Failed to fetch');
		expect(labelCount('Couldn’t check')).toBe(1);
		expect(host.querySelector('.status-row.stale .dotmark')).toBeNull();
		expect(warn).toHaveBeenCalledWith('Could not refresh host access', expect.any(TypeError));
	});
	// A decision that fails (no connection, a refusal, a 409) shows its one drawer line from decisionFailure;
	// those cases are pinned by the decision-failure tests above.
});

it('offers Until I turn it off on a saved-login request only, never on a host-access lease request', async () => {
	const { default: HostAccessPendingRequestDrawer } = await import('./HostAccessPendingRequestDrawer.svelte');
	const fill: HostAccessFill = {
		id: 'fill-1', kind: 'fill', state: 'awaiting_user', thread_id: 'thread-1', root_thread_id: 'thread-1',
		agent: 'Bookkeeper', site: 'https://example.com', domain: 'example.com', username: 'me@example.com',
		revision: 1, application_hash: 'hash-fill', decision_capability: 'decision-fill',
		max_duration_seconds: 1800, requested_duration_seconds: 900, duration_seconds: null, standing: false, expires_at: null,
		created_at: '2026-07-18T16:00:00+00:00', updated_at: '2026-07-18T16:00:00+00:00',
	};
	const seen: Record<string, string> = {};
	for (const lease of [waitingLease, fill] as HostAccessItem[]) {
		const target = document.createElement('div');
		document.body.appendChild(target);
		const drawer = mount(HostAccessPendingRequestDrawer, { target, props: {
			lease, stale: false, deciding: false, rejectDisabled: false, approveDisabled: false,
			onApprove: vi.fn(), onReject: vi.fn(), onRetry: vi.fn(), onClose: vi.fn(),
		} });
		flushSync();
		seen[lease.kind ?? 'lease'] = target.textContent ?? '';
		await unmount(drawer);
		target.remove();
	}
	expect(seen.lease).toContain('5 minutes');
	expect(seen.lease).not.toContain('Until I turn it off');
	expect(seen.fill).toContain('Until I turn it off');
});
