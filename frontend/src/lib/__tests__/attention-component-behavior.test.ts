/**
 * L2 component-behavior tests for the S6b attention surface.
 *
 * These mount the actual Svelte components and mock only the HTTP API boundary.
 * They intentionally avoid copying component state-machine logic into tests.
 */

import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import AttentionCard from '$lib/components/AttentionCard.svelte';
import AttentionList from '$lib/components/AttentionList.svelte';
import {
	clearAttention,
	disposeAttention,
	fetchRaised,
	fetchReview,
	type AttentionItem,
	type ReviewResult,
} from '$lib/api/attention';
import { token } from '$lib/stores/auth';

vi.mock('$lib/api/attention', () => ({
	clearAttention: vi.fn(),
	disposeAttention: vi.fn(),
	fetchRaised: vi.fn(),
	fetchReview: vi.fn(),
}));

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

const HIGH: AttentionItem = {
	external_id: 'high',
	integration: 'github',
	type: 'pull-request',
	attention_state: 'raised',
	urgency: 'high',
	requested_action: 'High action',
	owner_thread: 'IMPL · Coordination MVP',
	levels: [],
};

const MED: AttentionItem = {
	...HIGH,
	external_id: 'medium',
	urgency: 'medium',
	requested_action: 'Medium action',
};

const LOW: AttentionItem = {
	...HIGH,
	external_id: 'low',
	urgency: 'low',
	requested_action: 'Low action',
};

const HIGH_DEFERRED: AttentionItem = {
	...HIGH,
	external_id: 'high-deferred',
	attention_state: 'deferred',
	requested_action: 'Deferred action',
	defer_condition: { type: 'deadline', value: '2026-07-01T09:00:00+00:00' },
};

const BLOCKING_REVIEW: ReviewResult = {
	artifact_id: 'high',
	blocking_failure: true,
	may_approve_to_user: false,
	recommendation: 'request_revision',
	findings: [{ severity: 'blocking', message: 'Missing evidence' }],
};

const CLEAN_REVIEW: ReviewResult = {
	artifact_id: 'high',
	blocking_failure: false,
	may_approve_to_user: true,
	recommendation: 'approve_to_user',
	findings: [],
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	vi.mocked(clearAttention).mockReset();
	vi.mocked(disposeAttention).mockReset();
	vi.mocked(fetchRaised).mockReset();
	vi.mocked(fetchReview).mockReset();
	vi.mocked(clearAttention).mockResolvedValue({
		artifact_id: 'high',
		outcome: 'answer',
		attention_state: 'cleared',
		cleared_levels: [],
		artifact_archived: false,
		subscription_archived: false,
	});
	vi.mocked(disposeAttention).mockResolvedValue({
		artifact_id: 'high',
		outcome: 'approve_to_user',
		user_visible: true,
		attention_state: 'raised',
	});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.restoreAllMocks();
});

async function settle() {
	await tick();
	await Promise.resolve();
	flushSync();
}

function mountList(items: AttentionItem[]) {
	vi.mocked(fetchRaised).mockResolvedValue({ items });
	component = mount(AttentionList, { target: host, props: { mode: 'user' } });
	flushSync();
}

function mountCard(
	item: AttentionItem,
	props: Partial<{
		mode: 'user' | 'parent' | 'readonly';
		demoReview: ReviewResult | null;
		onDone: (artifactId: string, outcome: string) => void;
	}> = {},
) {
	component = mount(AttentionCard, {
		target: host,
		props: { item, mode: 'user', ...props },
	});
	flushSync();
}

function button(label: string): HTMLButtonElement {
	const match = Array.from(host.querySelectorAll('button')).find(
		(el) => el.textContent?.trim() === label || el.getAttribute('aria-label') === label,
	) as HTMLButtonElement | undefined;
	expect(match).toBeTruthy();
	return match!;
}

function textButton(label: string): HTMLButtonElement {
	const match = Array.from(host.querySelectorAll('button')).find(
		(el) => el.textContent?.trim() === label,
	) as HTMLButtonElement | undefined;
	expect(match).toBeTruthy();
	return match!;
}

describe('AttentionList mounted behavior', () => {
	it('renders fetched items sorted by urgency, with raised before deferred in the same tier', async () => {
		mountList([LOW, HIGH_DEFERRED, MED, HIGH]);
		await settle();

		const actions = Array.from(host.querySelectorAll('.requested-action')).map(
			(el) => el.textContent?.trim(),
		);
		expect(actions).toEqual([
			'High action',
			'Deferred action',
			'Medium action',
			'Low action',
		]);
	});
});

describe('AttentionCard mounted user actions', () => {
	it('clears explicitly through the Answer button and calls onDone', async () => {
		const onDone = vi.fn();
		mountCard(HIGH, { onDone });

		button('Answer').click();
		await settle();

		expect(clearAttention).toHaveBeenCalledWith('tok', 'high', 'answer', undefined);
		expect(onDone).toHaveBeenCalledWith('high', 'answer');
	});

	it('opens the defer modal and submits a canonical UTC deadline', async () => {
		mountCard(HIGH);

		button('Defer').click();
		await settle();
		const input = host.querySelector<HTMLInputElement>('input[type="datetime-local"]');
		expect(input).toBeTruthy();
		input!.value = '2026-07-01T00:00';
		input!.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		textButton('Defer').click();
		await settle();

		expect(clearAttention).toHaveBeenCalledWith('tok', 'high', 'defer', {
			condition: {
				type: 'deadline',
				value: new Date('2026-07-01T00:00').toISOString(),
			},
		});
	});

	it('routes overflow Supersede through the explicit clear action', async () => {
		mountCard(HIGH);

		button('More options').click();
		await settle();
		button('Supersede').click();
		await settle();

		expect(clearAttention).toHaveBeenCalledWith('tok', 'high', 'supersede', undefined);
	});
});

describe('AttentionCard mounted parent disposition', () => {
	it('shows a blocking review before click and disables Approve', async () => {
		mountCard(HIGH, { mode: 'parent', demoReview: BLOCKING_REVIEW });
		await settle();

		const approve = button('Approve');
		expect(approve.disabled).toBe(true);
		expect(approve.title).toBe('Blocked: review profile has a blocking issue');
		expect(host.textContent).toContain('Review profile flagged a blocking issue');
		expect(host.textContent).toContain('Missing evidence');
		expect(disposeAttention).not.toHaveBeenCalled();
	});

	it('allows clean Approve and calls the dispose endpoint', async () => {
		const onDone = vi.fn();
		mountCard(HIGH, { mode: 'parent', demoReview: CLEAN_REVIEW, onDone });
		await settle();

		const approve = button('Approve');
		expect(approve.disabled).toBe(false);
		approve.click();
		await settle();

		expect(disposeAttention).toHaveBeenCalledWith(
			'tok',
			'high',
			'approve_to_user',
			undefined,
		);
		expect(onDone).toHaveBeenCalledWith('high', 'approve_to_user');
	});

	it('keeps deferred blocking items approvable per the parent-disposition rules', async () => {
		mountCard(HIGH_DEFERRED, { mode: 'parent', demoReview: BLOCKING_REVIEW });
		await settle();

		expect(button('Approve').disabled).toBe(false);
	});
});
