import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import EventChip from './EventChip.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';

const PR_CHIP_MSG = {
	id: 'e1',
	role: 'system',
	content: 'o/r#42 opened: Fix the thing',
	metadata: {
		type: 'context_event',
		event_type: 'webhook.github.pr_opened',
		summary: 'o/r#42 opened: Fix the thing',
		url: 'https://github.com/o/r/pull/42',
	},
};

const COMMENT_CHIP_MSG = {
	id: 'e2',
	role: 'system',
	content: 'o/r#42 comment from alice: lgtm',
	metadata: {
		type: 'context_event',
		event_type: 'webhook.github.pr_comment',
		summary: 'o/r#42 comment from alice: lgtm',
		url: 'https://github.com/o/r/pull/42#issuecomment-1',
	},
};

const CHECK_CHIP_MSG = {
	id: 'e3',
	role: 'system',
	content: 'webhook.github.check_run_completed on o/r#10',
	metadata: {
		type: 'context_event',
		event_type: 'webhook.github.check_run_completed',
		summary: 'webhook.github.check_run_completed on o/r#10',
		url: null,
	},
};

const RESTART_WAKE_MSG = {
	id: 'restart-wake-1',
	thread_id: 'thread-restart-wake',
	role: 'system',
	content: '[EVENT: system.restart.resume]',
	metadata: {
		type: 'context_event',
		event_type: 'system.restart.resume',
		event_disposition: 'wake',
		summary: 'Instance restarted — resuming registered work',
		url: null,
	},
	delivery_state: 'queued',
	delivery_error: null,
	delivery_revision: 1,
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('ui-token');
	activeThreadId.set(null);
	messages.set([]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	vi.unstubAllGlobals();
});

function renderChip(msg: unknown): HTMLElement {
	component = mount(EventChip, { target: host, props: { msg } });
	flushSync();
	const chip = host.querySelector<HTMLElement>('.ev-chip');
	expect(chip).not.toBeNull();
	return chip!;
}

describe('EventChip', () => {
	it('uses one click disclosure for an aggregated context event', () => {
		const chip = renderChip({
			id: 'context-digest', role: 'system',
			content: '[PASSIVE CONTEXT DIGEST]\n2 context updates\n- Plan paused.',
			delivery_state: 'queued',
			metadata: {
				type: 'context_event', event_type: 'context.digest',
				event_disposition: 'passive', summary: '2 context updates',
				context_events: [
					{ id: 'one', summary: 'Plan paused.', created_at: '2026-08-28T12:00:00Z' },
					{ id: 'two', summary: 'Review requested.', created_at: '2026-08-28T12:01:00Z' },
				],
			},
		});

		expect(chip.dataset.contextDigest).toBe('pending');
		expect(chip.querySelector('.ev-summary')?.textContent).toBe('2 context updates');
		const disclosure = host.querySelector<HTMLButtonElement>('[data-event-disclosure]');
		expect(disclosure?.getAttribute('aria-expanded')).toBe('false');
		expect(host.querySelector('.ev-details')).toBeNull();

		disclosure?.click();
		flushSync();

		expect(disclosure?.getAttribute('aria-expanded')).toBe('true');
		expect(host.querySelectorAll('.ev-detail')).toHaveLength(2);
		expect(host.textContent).toContain('Plan paused.');
		expect(host.textContent).toContain('Review requested.');
	});

	it('renders PR opened chip with summary and link', () => {
		const chip = renderChip(PR_CHIP_MSG);
		expect(chip.querySelector('.ev-label')?.textContent).toBe('PR');
		expect(chip.querySelector('.ev-summary')?.textContent).toBe('o/r#42 opened: Fix the thing');
		expect((chip as HTMLAnchorElement).href).toContain('/pull/42');
		expect(chip.querySelector('.ev-link')?.textContent).toBe('↗');
		expect(host.textContent).not.toContain('"event_type"');
	});

	it('makes the entire chip row clickable when a safe url exists', () => {
		const chip = renderChip(PR_CHIP_MSG);
		expect(chip.tagName).toBe('A');
		expect((chip as HTMLAnchorElement).href).toBe('https://github.com/o/r/pull/42');
		expect(chip.getAttribute('target')).toBe('_blank');
		expect(chip.getAttribute('rel')).toBe('noopener noreferrer');
	});

	it('renders comment chip with hash glyph', () => {
		const chip = renderChip(COMMENT_CHIP_MSG);
		expect(chip.querySelector('.ev-label')?.textContent).toBe('PR comment');
		expect(chip.querySelector('.ev-summary')?.textContent).toContain('alice');
	});

	it('renders check_run chip with neutral circle glyph', () => {
		const chip = renderChip(CHECK_CHIP_MSG);
		expect(chip.querySelector('.ev-label')?.textContent).toBe('Check');
	});

	it('renders an installed checkpoint as one quiet completion receipt', () => {
		const chip = renderChip({
			id: 'checkpoint-receipt:cp-1', role: 'system', content: 'Context saved',
			metadata: {
				type: 'activity_receipt', event_type: 'checkpoint.installed',
				summary: 'Context saved', checkpoint_id: 'cp-1',
			},
		});
		expect(chip.querySelector('.ev-label')?.textContent).toBe('Checkpoint');
		expect(chip.querySelector('.ev-summary')?.textContent).toBe('Context saved');
	});

	it('renders no link when url is null', () => {
		const chip = renderChip(CHECK_CHIP_MSG);
		expect(chip.tagName).toBe('DIV');
		expect(chip.querySelector('.ev-link')).toBeNull();
	});

	it('falls back to msg.content when summary is missing', () => {
		const chip = renderChip({
			id: 'e4', role: 'system',
			content: 'fallback summary',
			metadata: { type: 'context_event', event_type: 'webhook.github.pr_opened' },
		});
		expect(chip.querySelector('.ev-summary')?.textContent).toBe('fallback summary');
	});

	it('does not render a link for javascript: scheme URLs (XSS guard)', () => {
		const chip = renderChip({
			id: 'e5', role: 'system',
			content: 'malicious chip',
			metadata: {
				type: 'context_event',
				event_type: 'webhook.github.pr_opened',
				summary: 'malicious chip',
				url: 'javascript:alert(1)',
			},
		});
		expect(chip.querySelector('.ev-link')).toBeNull();
	});

	it('cancels a queued restart wake without reclassifying it as a user message', async () => {
		activeThreadId.set(RESTART_WAKE_MSG.thread_id);
		messages.set([RESTART_WAKE_MSG]);
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				thread_id: RESTART_WAKE_MSG.thread_id,
				message: {
					...RESTART_WAKE_MSG,
					delivery_state: 'cancelled',
					delivery_error: 'cancelled_by_user',
					delivery_revision: 2,
				},
			}),
		});
		vi.stubGlobal('fetch', fetchMock);

		const chip = renderChip(RESTART_WAKE_MSG);
		expect(chip.querySelector('.ev-label')?.textContent).toBe('Restart');
		const cancel = host.querySelector<HTMLButtonElement>('[data-delivery-action="cancel"]');
		expect(cancel?.textContent).toBe('Cancel');
		cancel?.click();

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				'/api/threads/thread-restart-wake/messages/restart-wake-1/cancel?token=ui-token',
				{ method: 'POST' },
			);
		});
		await vi.waitFor(() => {
			expect(get(messages)).toEqual([
				expect.objectContaining({
					id: 'restart-wake-1',
					role: 'system',
					delivery_state: 'cancelled',
					delivery_error: 'cancelled_by_user',
					delivery_revision: 2,
				}),
			]);
		});
	});

	it('offers Retry on a wake parked on our own defect, and no Retry otherwise', async () => {
		// A parked head is excluded from the recovery sweep by design, so the
		// user's Retry is its only exit. This chip previously offered Cancel
		// alone, which left an undeliverable wake blocking its whole queue.
		activeThreadId.set(RESTART_WAKE_MSG.thread_id);

		renderChip(RESTART_WAKE_MSG);
		expect(host.querySelector('[data-delivery-action="retry"]')).toBeNull();

		host.innerHTML = '';
		const parked = { ...RESTART_WAKE_MSG, delivering_reason: 'delivery_defect' };
		messages.set([parked]);
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				thread_id: parked.thread_id,
				retried: true,
				released_defect: true,
				message: { ...parked, delivering_reason: null, delivery_revision: 2 },
			}),
		});
		vi.stubGlobal('fetch', fetchMock);

		renderChip(parked);
		const retry = host.querySelector<HTMLButtonElement>('[data-delivery-action="retry"]');
		expect(retry?.textContent).toBe('Retry');
		retry?.click();

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				expect.stringContaining(
					'/api/threads/thread-restart-wake/messages/restart-wake-1/retry',
				),
				{ method: 'POST' },
			);
		});
		// The response carries the projection, so the released row is adopted.
		await vi.waitFor(() => {
			expect(get(messages)).toEqual([
				expect.objectContaining({ id: 'restart-wake-1', delivering_reason: null }),
			]);
		});
	});

	it.each([
		['failed', 'retry', 'Retry'],
		['ambiguous', 'retry-anyway', 'Retry anyway'],
	] as const)(
		'offers the canonical recovery action for a %s wake',
		(deliveryState, action, label) => {
			activeThreadId.set(RESTART_WAKE_MSG.thread_id);
			renderChip({
				...RESTART_WAKE_MSG,
				delivery_state: deliveryState,
				delivery_error: deliveryState === 'ambiguous'
					? 'provider_write_ambiguous'
					: 'legacy queued input targeted an obsolete model',
			});

			const retry = host.querySelector<HTMLButtonElement>(
				`[data-delivery-action="${action}"]`,
			);
			expect(retry?.textContent).toBe(label);
			expect(host.querySelector('[data-delivery-action="cancel"]')).toBeNull();
		},
	);

	it('does not offer Retry for a cancelled wake', () => {
		activeThreadId.set(RESTART_WAKE_MSG.thread_id);
		renderChip({
			...RESTART_WAKE_MSG,
			delivery_state: 'cancelled',
			delivery_error: 'cancelled_by_user',
		});

		expect(host.textContent).toContain('Cancelled by you');
		expect(host.querySelector('[data-delivery-action]')).toBeNull();
	});

	it('does not offer delivery actions for a passive digest event', () => {
		renderChip({
			...RESTART_WAKE_MSG,
			id: 'digest-1',
			metadata: {
				...RESTART_WAKE_MSG.metadata,
				event_type: 'context.digest',
				event_disposition: 'digest',
				summary: 'Passive digest',
			},
		});

		expect(host.querySelector('[data-delivery-action="cancel"]')).toBeNull();
	});
	it('reads a zone-less server timestamp as UTC and shows the viewer\'s local time', () => {
		const zone = 'America/Los_Angeles';
		const previousTz = process.env.TZ;
		process.env.TZ = zone;
		try {
			renderChip({
				id: 'digest-times', role: 'system', content: '[PASSIVE CONTEXT DIGEST]\n2 context updates',
				metadata: {
					type: 'context_event', event_type: 'context.digest',
					event_disposition: 'passive', summary: '2 context updates',
					context_events: [
						{ id: 'naive', summary: 'Plan paused', created_at: '2026-09-18T21:24:00.123456' },
						{ id: 'zoned', summary: 'Review requested', created_at: '2026-09-18T22:13:00+00:00' },
					],
				},
			});
			host.querySelector<HTMLButtonElement>('[data-event-disclosure]')?.click();
			flushSync();

			const shown = [...host.querySelectorAll('.ev-detail time')].map((t) => t.textContent);
			const localTime = (iso: string) => new Date(iso).toLocaleTimeString(
				[], { hour: 'numeric', minute: '2-digit', timeZone: zone },
			);
			expect(shown).toEqual([
				localTime('2026-09-18T21:24:00Z'), localTime('2026-09-18T22:13:00Z'),
			]);
			expect(shown[0]).not.toBe(
				new Date('2026-09-18T21:24:00').toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
			);
		} finally {
			if (previousTz === undefined) delete process.env.TZ;
			else process.env.TZ = previousTz;
		}
	});
});
