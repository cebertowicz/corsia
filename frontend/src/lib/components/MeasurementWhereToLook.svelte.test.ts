import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementWhereToLook from './MeasurementWhereToLook.svelte';
import source from './MeasurementWhereToLook.svelte?raw';
import {
	decodeWhereToLook,
	formatChange,
	formatWhereToLookValue,
	type WhereToLookRow,
	type WhereToLookState,
} from '$lib/stores/measurementWhereToLook';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

const row = (over: Partial<WhereToLookRow> = {}): WhereToLookRow => ({
	subject: { id: 'ag-1', name: 'Bugs', kind: 'agent' },
	measure: 'first_try', label: 'First try', format: 'percent',
	previous: 0.8, latest: 0.6, change: -0.25, n: 40, overall: 0.7,
	action: 'Read the last rejected outcomes.', ...over,
});

const state = (over: Partial<WhereToLookState> = {}): WhereToLookState => ({
	loading: false, error: null, worse: [], improved: [], period: { grain: 'weeks' as const, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-07-13', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } },
	minimumN: 20, scopeKey: 'all:none', ...over,
});

const css = source.slice(source.indexOf('<style>'));

/** The body of one `@media` block in the component's style sheet, braces balanced. */
function media(query: string): string {
	const start = css.indexOf(`@media ${query}`);
	expect(start, `the sheet has an @media ${query} block`).toBeGreaterThan(-1);
	const open = css.indexOf('{', start);
	let depth = 0;
	for (let at = open; at < css.length; at++) {
		if (css[at] === '{') depth++;
		if (css[at] === '}' && --depth === 0) return css.slice(open + 1, at);
	}
	throw new Error(`unbalanced @media ${query}`);
}

function render(value: WhereToLookState, onRetry = () => {}): void {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementWhereToLook, { target: host, props: { state: value, onRetry } });
	flushSync();
}

describe('MeasurementWhereToLook', () => {
	it('shows both lists with the fixed action line under each measure', () => {
		render(state({ worse: [row()], improved: [row({ subject: { id: 'ag-2', name: 'Docs', kind: 'agent' }, previous: 0.5, latest: 0.7, change: 0.4 })] }));
		const text = host.textContent ?? '';
		expect(text).toContain('Largest changes for the worse');
		expect(text).toContain('Improved');
		expect(text).toContain('Bugs');
		expect(text).toContain('First try');
		expect(text).toContain('80% to 60%');
		expect(text).toContain('-25%');
		expect(text).toContain('+40%');
		expect(host.querySelector('.act')?.textContent).toBe('Read the last rejected outcomes.');
	});

	it('says what the threshold is rather than showing an empty table', () => {
		render(state());
		expect(host.querySelectorAll('table')).toHaveLength(0);
		expect(host.textContent).toContain('Nothing got worse by 10% or more with at least 20 outcomes (or turns, or tasks) in both Sep 28 to Oct 4 and Sep 21 to Sep 27.');
		expect(host.querySelector('.stat')?.textContent?.trim()).toBe('Sep 28 to Oct 4 against Sep 21 to Sep 27, worst change first');
	});

	it('leaves out an Improved list with no rows, as the render does, and shows it once it has some', () => {
		render(state());
		expect([...host.querySelectorAll('h3')].map((h) => h.textContent)).toEqual(['Largest changes for the worse']);
		expect(host.textContent).not.toContain('Improved');
		unmount(component!); host.remove();
		render(state({ improved: [row({ change: 0.25 })] }));
		expect([...host.querySelectorAll('h3')].map((h) => h.textContent)).toEqual(['Largest changes for the worse', 'Improved']);
	});

	it('capitalises an agent subject and leaves a scope subject as served', () => {
		render(state({ worse: [
			row({ subject: { id: 'a1', name: 'conductor', kind: 'agent' } }),
			row({ subject: { id: 'p1', name: 'corsia', kind: 'scope' }, measure: 'm2' }),
		] }));
		const subjects = [...host.querySelectorAll('tbody td.subj b')].map((node) => node.textContent);
		expect(subjects).toEqual(['Conductor', 'corsia']);
	});

	it('shows an en dash in Overall for a scope row, never zero or blank', () => {
		render(state({ worse: [row({ subject: { id: 'all', name: 'All work', kind: 'scope' }, overall: null })] }));
		const cells = [...host.querySelectorAll('tbody td.n')].map((cell) => cell.textContent);
		expect(cells.at(-1)).toBe('–');
		expect(host.querySelector('tbody .kind')).toBeNull();
	});

	it('stacks the subject above its measure in one cell at 430px and under', () => {
		render(state({ worse: [row()] }));
		const subject = host.querySelector('tbody td.subj');
		const stacked = subject?.querySelector('div.stacked');
		expect(subject?.querySelector('b')?.textContent).toBe('Bugs');
		expect(stacked?.textContent, 'the measure and its action sit under the subject').toBe('First tryRead the last rejected outcomes.');
		expect(subject?.firstElementChild?.tagName, 'the subject comes first').toBe('B');
		expect(host.querySelector('thead th')?.textContent).toBe('Subject and measure');
		// Wide, the measure keeps its own column and the stacked copy is hidden; narrow, the reverse.
		expect(host.querySelector('tbody td.measure')?.textContent).toBe('First tryRead the last rejected outcomes.');
		expect(css).toMatch(/\n\t\.stacked \{ display: none; \}/);
		const phone = media('(max-width: 430px)');
		expect(phone).toContain('.tb th.measure, .tb td.measure { display: none; }');
		expect(phone).toContain('span.stacked { display: inline; }');
		expect(phone).toMatch(/div\.stacked \{ display: block;/);
	});

	it('fits the card at 430px and under: no width floor on the subject column, headers wrap at spaces, the long-name rule is the subject alone', () => {
		const phone = media('(max-width: 430px)');
		expect(phone).toContain('.tb td:first-child, .tb th:first-child { min-width: 0; }');
		expect(phone).toContain('.tb th { white-space: normal; word-break: normal; }');
		expect(phone).toContain('.tb td.subj b { overflow-wrap: anywhere; }');
		// Outside the phone block the subject column keeps its 10ch floor.
		expect(css).toContain('.tb td:first-child, .tb th:first-child { min-width: 10ch; }');
		expect(phone).not.toContain('table-layout');
	});

	it('gives the worse and improved tables the same fixed column widths on a wide screen', () => {
		render(state({ worse: [row()], improved: [row({ subject: { id: 'ag-2', name: 'Docs', kind: 'agent' }, change: 0.4 })] }));
		const columns = [...host.querySelectorAll('table.tb')].map((table) => [...table.querySelectorAll('colgroup col')].map((col) => [...col.classList].find((name) => name.startsWith('c-'))));
		expect(columns).toHaveLength(2);
		expect(columns[0]).toEqual(['c-subject', 'c-measure', 'c-values', 'c-change', 'c-n', 'c-overall']);
		expect(columns[1]).toEqual(columns[0]);
		const wide = media('(min-width: 1024px)');
		expect(wide).toContain('.tb { table-layout: fixed; }');
		const widths = columns[0].map((name) => Number(name && wide.match(new RegExp(`\\.${name} \\{ width: (\\d+)%; \\}`))?.[1]));
		expect(widths.every((width) => width > 0), 'every column has a width').toBe(true);
		expect(widths.reduce((sum, width) => sum + width, 0)).toBe(100);
	});

	it('renders a failed first load as a failure with a retry', () => {
		const retry = vi.fn();
		render(state({ period: null, error: 'Could not load the biggest changes.' }), retry);
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('Could not load the biggest changes.');
		host.querySelector('button')?.click();
		expect(retry).toHaveBeenCalledOnce();
		expect(host.querySelectorAll('table')).toHaveLength(0);
	});

	it('keeps the served rows when a reload fails', () => {
		render(state({ worse: [row()], error: 'Could not load the biggest changes.' }));
		expect(host.querySelector('table')).not.toBeNull();
		expect(host.querySelector('[role="alert"]')).not.toBeNull();
	});

	it('carries no tracker or plan wording in its text', () => {
		render(state({ worse: [row()] }));
		expect(host.textContent).not.toMatch(/#\d|issue|plan/i);
	});
});

describe('where-to-look store helpers', () => {
	it('formats each measure in its own unit', () => {
		expect(formatWhereToLookValue(0.756, 'percent')).toBe('76%');
		expect(formatWhereToLookValue(7_440_000_000, 'tokens')).toBe('7.4B');
		expect(formatWhereToLookValue(2.345, 'hours')).toBe('2.3 h');
		expect(formatWhereToLookValue(12.34, 'minutes')).toBe('12.3');
		expect(formatWhereToLookValue(1.236, 'ratio')).toBe('1.24');
		expect(formatWhereToLookValue(1.964, 'dollars')).toBe('$1.96');
		expect(formatChange(0.1)).toBe('+10%');
		expect(formatChange(-0.256)).toBe('-26%');
	});

	it('refuses a response with no lists or a bad number', () => {
		expect(() => decodeWhereToLook({})).toThrow();
		expect(() => decodeWhereToLook({ worse: [{ subject: { id: 'a', name: 'A' }, measure: 'm', label: 'l', action: 'x', format: 'percent', previous: null, latest: 1, change: 1, n: 1 }], improved: [], minimum_n: 20 })).toThrow();
	});

	it('refuses a response with no period instead of naming one', () => {
		expect(() => decodeWhereToLook({ worse: [], improved: [], minimum_n: 20 })).toThrow('period shape');
	});

	it('reads a model subject and a dollars row, and shows no Agent or Model tag under the subject', () => {
		const model = { subject: { id: 'opus', name: 'Opus 5.5', kind: 'model' }, measure: 'cost_per_task', label: 'M $ per task', action: 'x', format: 'dollars', previous: 8, latest: 12, change: 0.5, n: 40, overall: 8.5 };
		const decoded = decodeWhereToLook({
			worse: [model], improved: [], period: { grain: 'weeks' as const, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-07-13', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } }, minimum_n: 20,
		});
		expect(decoded.worse[0].subject.kind).toBe('model');
		render(state({ worse: decoded.worse }));
		const text = host.textContent ?? '';
		expect(text).toContain('Opus 5.5');
		expect(host.querySelector('.kind')).toBeNull();
		expect(text).toContain('M $ per task');
		expect(text).toContain('$8.00 to $12.00');
		expect(text).toContain('$8.50');
	});

	it('keeps an absent overall as null', () => {
		const decoded = decodeWhereToLook({
			worse: [{ subject: { id: 's', name: 'All work', kind: 'scope' }, measure: 'm', label: 'l', action: 'x', format: 'percent', previous: 0.5, latest: 0.4, change: -0.2, n: 30, overall: null }],
			improved: [], period: { grain: 'weeks' as const, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-07-13', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } }, minimum_n: 20,
		});
		expect(decoded.worse[0].overall).toBeNull();
		expect(decoded.worse[0].subject.kind).toBe('scope');
	});
});
