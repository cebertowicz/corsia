import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { activeToolsByThread } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { DAY, THREAD, notice, present, said, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import TranscriptContent from './TranscriptContent.svelte';
import type { TranscriptEntry, TranscriptMessage } from './messageListModel';
import { _resetLiveStepForTests } from './plans/planLiveStep';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(entries: TranscriptEntry[]): void {
	component = mount(TranscriptContent, {
		target: host,
		props: {
			entries, turnSummaries: { t1: turn('10:00:00', '10:10:00', 2) }, renderedStreaming: false, historyComplete: true,
			expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			threadId: THREAD,
		},
	});
	flushSync();
}

const toggle = () => host.querySelector<HTMLButtonElement>('.fold-toggle')!;

function markNew(entries: TranscriptEntry[]): TranscriptEntry[] {
	return entries.map((entry) => (entry.kind === 'fold' ? { ...entry, new: true } : entry));
}

function recoveredError(): TranscriptMessage {
	return { ...notice('err', 3, '10:02:30'), content: 'Provider hiccup', metadata: { type: 'cc_error', recovered: true } };
}

function contextUpdate(): TranscriptMessage {
	return {
		id: 'ctx', thread_id: THREAD, role: 'system', content: 'Plan changed', seq: 4, created_at: `${DAY}10:03:00`,
		activity_weight: 0, metadata: { type: 'context_event', turn_id: 't1' },
	};
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	_resetLiveStepForTests();
	activeToolsByThread.set(new Map());
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
});

describe('WorkBlock structure', () => {
	it('is a closed capsule header with no body until it is opened', () => {
		render(present([work('a', 1, '10:01:00'), work('b', 2, '10:02:00'), said('reply', 5, '10:10:00')]));

		const block = host.querySelector('[data-work-block]')!;
		expect(block).not.toBeNull();
		expect(block.hasAttribute('data-new')).toBe(false);
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		expect(toggle().getAttribute('aria-controls')).toBeNull();
		expect(host.querySelector('.fold-details')).toBeNull();
		expect(toggle().textContent).toContain('Worked');
	});

	it('opens a body the header controls, and closes it again', () => {
		render(present([work('a', 1, '10:01:00'), said('reply', 5, '10:10:00')]));

		toggle().click();
		flushSync();

		const body = host.querySelector('.fold-details')!;
		expect(toggle().getAttribute('aria-expanded')).toBe('true');
		expect(toggle().getAttribute('aria-controls')).toBe(body.id);
		expect(toggle().getAttribute('aria-label')).toMatch(/^Collapse /);

		toggle().click();
		flushSync();
		expect(host.querySelector('.fold-details')).toBeNull();
	});
});

describe('WorkBlock lines', () => {
	it('shows a context update and a recovered error as quiet lines, the error in red', () => {
		render(present([work('a', 1, '10:01:00'), work('b', 2, '10:02:00'), recoveredError(), contextUpdate(), said('reply', 6, '10:10:00')]));

		toggle().click();
		flushSync();

		const error = host.querySelector('[data-block-line="error_recovered"]')!;
		expect(error.textContent).toContain('Error: Provider hiccup');
		expect(error.classList.contains('error')).toBe(true);
		const context = host.querySelector('[data-block-line="context_update"]')!;
		expect(context.classList.contains('error')).toBe(false);
		expect(context.querySelector('time')).not.toBeNull();
	});
});

describe('WorkBlock new marker (F2)', () => {
	it('marks a new collapsed block with one capsule inside the chip, and no divider beside it', () => {
		const entries = markNew(present([work('a', 1, '10:01:00'), said('reply', 5, '10:10:00')]))
			.map((entry) => entry.kind === 'fold' ? { ...entry, divider: { truncated: false, first_seq: 1 } } : entry);
		render(entries);

		const block = host.querySelector('[data-work-block]')!;
		expect(block.hasAttribute('data-new')).toBe(true);
		expect(host.querySelectorAll('[data-new-capsule]')).toHaveLength(1);
		expect(toggle().querySelector('[data-new-capsule] i')).not.toBeNull();
		expect(toggle().querySelector('[data-new-capsule]')?.textContent).toMatch(/^·\s*new$/);
		expect(toggle().textContent?.match(/new/g)).toHaveLength(1);
		expect(host.querySelector('[data-visit-divider]')).toBeNull();
		expect(toggle().getAttribute('aria-label')).toMatch(/^Expand .*, new since your last visit$/);
	});

	it('shows no capsule on a live block', () => {
		const entries = markNew(present([work('a', 1, '10:01:00'), said('reply', 5, '10:10:00')]))
			.map((entry) => entry.kind === 'fold' ? { ...entry, live: true } : entry);
		render(entries);

		expect(host.querySelector('[data-work-block]')?.classList.contains('live-work')).toBe(true);
		expect(host.querySelector('[data-new-capsule]')).toBeNull();
	});

	it('moves that divider to the first new message only after the block opens', () => {
		const entries = markNew(present([work('a', 1, '10:01:00'), said('reply', 5, '10:10:00')]))
			.map((entry) => entry.kind === 'fold' ? { ...entry, divider: { truncated: false, first_seq: 1 } } : entry);
		render(entries);

		toggle().click();
		flushSync();

		const divider = host.querySelector('[data-visit-divider]')!;
		expect(host.querySelector('[data-work-block]')?.contains(divider)).toBe(true);
		expect(host.querySelector('.fold-details')?.contains(divider)).toBe(true);
	});

	it('has no marker when nothing in the block is new', () => {
		render(present([work('a', 1, '10:01:00'), said('reply', 5, '10:10:00')]));

		expect(host.querySelector('[data-new-capsule]')).toBeNull();
		expect(toggle().getAttribute('aria-label')).not.toContain('new since');
	});
});
