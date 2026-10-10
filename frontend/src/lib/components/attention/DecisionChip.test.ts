/**
 * Component tests for DecisionChip — issue #2001: a multi-line Other field,
 * a Recommended badge on its option, and brief/tradeoff above the options.
 * Mounted directly with Svelte 5's client `mount`/`unmount` in jsdom (the
 * vite config's `conditions: ['browser']` resolves Svelte's client entry
 * for this); no `@testing-library/svelte` dependency exists in this repo.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import DecisionChip from './DecisionChip.svelte';
import type { DecisionDetail } from '$lib/api/attentionQueue';
import { __reset } from '$lib/stores/attentionDock.svelte';

function makeDecision(overrides: Partial<DecisionDetail> = {}): DecisionDetail {
	return {
		id: 'dec-1',
		plan_id: 'plan-1',
		node_id: 'node-1',
		source_node_id: 'node-1',
		thread_id: 'thread-1',
		title: 'Pick an approach',
		context: 'Some context worth reading first.',
		tradeoff: 'Speed versus correctness.',
		recommendation: 'a',
		rationale: 'A is safer given the current evidence.',
		options: [
			{ id: 'a', label: 'Option A', detail: 'Detail A' },
			{ id: 'b', label: 'Option B', detail: 'Detail B' },
		],
		other_enabled: true,
		order_key: 0,
		revision: 1,
		raised_at: null,
		source: {
			thread_id: 'thread-1',
			thread_label: 'Thread',
			project_id: null,
			project_label: null,
			parent_reviewed: true,
		},
		...overrides,
	};
}

let host: HTMLElement | null = null;
let instance: Record<string, unknown> | null = null;

function renderChip(decision: DecisionDetail): HTMLElement {
	host = document.createElement('div');
	document.body.appendChild(host);
	instance = mount(DecisionChip, { target: host, props: { decision } });
	flushSync();
	return host;
}

afterEach(() => {
	if (instance) {
		unmount(instance);
		instance = null;
	}
	host?.remove();
	host = null;
	__reset();
});

describe('DecisionChip — Other field', () => {
	it('renders a real multi-line textarea, not a single-line input', () => {
		const el = renderChip(makeDecision());
		const textarea = el.querySelector('.other-option textarea');
		expect(textarea?.tagName).toBe('TEXTAREA');
		expect(el.querySelector('.other-option input')).toBeNull();
	});

	it('does not select Other merely by focusing the field', () => {
		const el = renderChip(makeDecision());
		const textarea = el.querySelector('textarea')!;
		textarea.focus();
		textarea.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
		flushSync();
		expect(el.querySelector('.other-option')!.classList.contains('selected')).toBe(false);
		const answerBtn = el.querySelector('.decision-actions button') as HTMLButtonElement;
		expect(answerBtn.disabled).toBe(true);
	});

	it('selects Other when the user types into the field', () => {
		const el = renderChip(makeDecision());
		const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
		textarea.value = 'a considered, multi-sentence answer.';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		expect(el.querySelector('.other-option')!.classList.contains('selected')).toBe(true);
	});

	it('does not submit on Enter — no form wraps it and nothing intercepts the key', () => {
		const el = renderChip(makeDecision());
		const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
		expect(textarea.closest('form')).toBeNull();
		textarea.value = 'line one';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		const enterEvent = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
		textarea.dispatchEvent(enterEvent);
		flushSync();
		expect(enterEvent.defaultPrevented).toBe(false);
		expect(el.querySelector('.receipt')!.textContent).not.toContain('recorded');
	});

	it('persists the draft on blur', () => {
		const el = renderChip(makeDecision());
		const textarea = el.querySelector('textarea') as HTMLTextAreaElement;
		textarea.value = 'draft text';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		textarea.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
		flushSync();
		unmount(instance!);
		instance = null;
		const el2 = renderChip(makeDecision());
		expect((el2.querySelector('textarea') as HTMLTextAreaElement).value).toBe('draft text');
	});
});

describe('DecisionChip — Recommended badge', () => {
	it('shows a Recommended badge on the matching option only', () => {
		const el = renderChip(makeDecision({ recommendation: 'b' }));
		const buttons = Array.from(el.querySelectorAll('.option-button'));
		const aBtn = buttons.find((b) => b.textContent?.includes('Option A'))!;
		const bBtn = buttons.find((b) => b.textContent?.includes('Option B'))!;
		expect(aBtn.textContent).not.toContain('Recommended');
		expect(bBtn.textContent).toContain('Recommended');
		expect(el.querySelector('dt')?.textContent).not.toBe('Recommendation');
	});

	it('shows no badge and no fallback row when no option matches the recommendation', () => {
		const el = renderChip(makeDecision({ recommendation: 'not-a-real-option-id' }));
		expect(el.textContent).not.toContain('Recommended');
		expect(el.textContent).not.toContain('None stated');
	});
});

describe('DecisionChip — layout order', () => {
	it('renders brief/tradeoff before the options in DOM order', () => {
		const el = renderChip(makeDecision());
		const top = el.querySelector('.decision-top')!;
		const options = el.querySelector('.options')!;
		const position = top.compareDocumentPosition(options);
		expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(top.textContent).toContain('Speed versus correctness.');
	});
});
