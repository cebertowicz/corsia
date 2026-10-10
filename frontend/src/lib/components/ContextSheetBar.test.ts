import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, describe, expect, it } from 'vitest';

import ContextSheet from './ContextSheet.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

function row(id: string, origin: string, tokens: number) {
	return {
		source_id: id, origin_section: origin, label: id, stable_reference: '',
		freshness: 'fresh', inclusion: 'included', owner_surface: '',
		metadata: {}, prompt_tokens: tokens,
	};
}

function usage(tokens: { system: number; project: number }) {
	return {
		model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
		history_tokens: 0, user_tokens: 0, total_used: tokens.system + tokens.project, utilization: 0.3,
		was_trimmed: false, history_messages: 0,
		context_snapshot: {
			snapshot_id: 'bar',
			source_inventory: [row('s', 'system', tokens.system), row('p', 'project', tokens.project)],
		},
	} as any;
}

function render(props: Record<string, unknown>) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ContextSheet, { target: host, props });
	flushSync();
}

describe('ContextSheet scope bar', () => {
	it('sizes each segment to its group total over the window and labels it for screen readers', () => {
		render({ usage: usage({ system: 200, project: 100 }) });
		const segments = [...host.querySelectorAll<HTMLElement>('.ctx-segment')];
		expect(segments.map((el) => el.style.width)).toEqual(['20%', '10%']);
		expect(segments[0].querySelector('.sr-only')?.textContent).toContain('System: 200 tokens, 20% of 1.0k');
		expect(host.querySelector('[role="img"] .sr-only')).toBeNull();
	});

	it('keeps a tiny group at its true width', () => {
		render({ usage: usage({ system: 1, project: 500 }) });
		const widths = [...host.querySelectorAll<HTMLElement>('.ctx-segment')].map((el) => el.style.width);
		expect(widths[0]).toBe('0.1%');
	});

	it('keeps an opened group open when the preview refreshes', () => {
		const current = new SvelteMap([['usage', usage({ system: 200, project: 100 })]]);
		render({ get usage() { return current.get('usage'); } });
		const group = host.querySelector<HTMLDetailsElement>('.ctx-source-group')!;
		group.open = true;
		group.dispatchEvent(new Event('toggle'));
		current.set('usage', usage({ system: 210, project: 100 }));
		flushSync();
		expect(host.querySelector<HTMLDetailsElement>('.ctx-source-group')?.open).toBe(true);
	});
});

describe('ContextSheet bar against the headline', () => {
	function barUsage(totalUsed: number) {
		const base = usage({ system: 200, project: 100 });
		return { ...base, total_used: totalUsed, utilization: totalUsed / 1000 };
	}

	it('splits the bar by group only when the rows add up to the headline, and then fills it exactly', () => {
		render({ usage: barUsage(300) });
		const widths = [...host.querySelectorAll<HTMLElement>('.ctx-segment')].map((el) => parseFloat(el.style.width));
		expect(widths.reduce((sum, width) => sum + width, 0)).toBeCloseTo(30, 6);
		expect(host.querySelector('.ctx-bar-note')).toBeNull();
		expect(host.querySelector('.sr-only')).not.toBeNull();
	});

	it('keeps the single headline bar and discloses the gap when the rows fall short', () => {
		render({ usage: barUsage(400) });
		expect(host.querySelector('.ctx-segment')).toBeNull();
		expect(host.querySelector<HTMLElement>('.ctx-fill')?.style.width).toBe('40%');
		expect(host.querySelector('.ctx-bar-note')?.textContent).toBe('Source estimates add up to 300, not the 400 total');
		expect(host.querySelector('.ctx-bar')?.getAttribute('aria-label')).toContain('add up to 300, not the 400 total');
		expect(host.querySelector('.sr-only')).toBeNull();
	});

	it('keeps the single headline bar and discloses the gap when the rows exceed the total', () => {
		render({ usage: barUsage(250) });
		expect(host.querySelector('.ctx-segment')).toBeNull();
		expect(host.querySelector<HTMLElement>('.ctx-fill')?.style.width).toBe('25%');
		expect(host.querySelector('.ctx-bar-note')?.textContent).toBe('Source estimates add up to 300, not the 250 total');
	});
});

describe('ContextSheet bar edge cases', () => {
	it('does not draw segments when an overcount row is negative, even if the groups sum to the total', () => {
		const base = usage({ system: 200, project: 100 });
		// Measured reading: 400 of estimates overcount a 300 total, so the delta row is -100.
		const measured = {
			...base,
			total_used: 300,
			utilization: 0.3,
			token_usage_available: true,
			token_source: 'measured',
			attribution_delta_tokens: -100,
			context_snapshot: {
				...base.context_snapshot,
				source_inventory: [
					row('s', 'system', 200),
					row('p', 'project', 200),
				],
			},
		};
		render({ usage: measured });
		expect(host.querySelector('.ctx-segment')).toBeNull();
		expect(host.querySelector<HTMLElement>('.ctx-fill')?.style.width).toBe('30%');
		expect(host.querySelector('.ctx-bar-note')?.textContent).toBe('Source estimates add up to 400, not the 300 total');
	});

	it('shows exact numbers when rounding would make them read alike', () => {
		const base = usage({ system: 40_000, project: 41_997 });
		render({ usage: { ...base, model_context_window: 262_144, total_used: 82_000, utilization: 0.31 } });
		const note = host.querySelector('.ctx-bar-note')?.textContent;
		expect(note).toBe('Source estimates add up to 81,997, not the 82,000 total');
		expect(host.querySelector('.ctx-bar')?.getAttribute('aria-label')).toBe(note);
	});
});
