import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementModels from './MeasurementModels.svelte';
import type { ModelsState } from '$lib/stores/measurementModels';

let component: Record<string, unknown> | null = null;
let host: HTMLElement | undefined;
Object.defineProperty(window, 'matchMedia', { configurable: true, value: (query: string) => ({ matches: false, media: query, addEventListener: () => {}, removeEventListener: () => {}, onchange: null }) });

afterEach(() => { if (component) unmount(component); host?.remove(); component = null; host = undefined; });
const row = { model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', tasks: 498, failed_turns_per_100: 2.7, recorded_cost_per_task: 2.3, spend_usd: 1145.4, cost_class: 'priced' as const, rate_reason: 'none' as const, cost_change: -0.13, change_reason: 'none' as const, previous_tasks: 500, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [] as ModelsState['rows'][number]['sizes'] };
const other = { ...row, model: '', label: 'Other (3 more models)', tasks: 9, recorded_cost_per_task: null, spend_usd: null, cost_class: 'no_pricing' as const, rate_reason: 'unpriced' as const, cost_change: null, change_reason: 'too_few_before' as const, previous_tasks: 4, other: true };
function state(overrides: Partial<ModelsState> = {}): ModelsState { return { loading: false, error: null, rows: [row], minTasks: 14, tokenShares: [{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', share_of_tokens: .4, tokens: 4_600_000_000 }, { model: 'codex/gpt-6.1-sol', label: 'GPT-6.1 Sol', share_of_tokens: .3, tokens: 9_800_000_000 }, { model: 'small', label: 'small', share_of_tokens: .3, tokens: 9_800_000_000 }], unmatchedFailedTurns: 0, unresolvedTokenShare: .026, totalSpendUsd: 1145.4, period: null, scopeKey: 'all:none', grain: 'days', ...overrides }; }
function render(overrides: Partial<ModelsState> = {}) { host = document.body.appendChild(document.createElement('div')); component = mount(MeasurementModels, { target: host, props: { state: state(overrides) } }); flushSync(); return host; }

describe('MeasurementModels', () => {
	it('shows one resolved model row with the approved columns and no source', () => {
		const page = render();
		expect(page.textContent).toContain('Claude Opus 5.5');
		expect(page.textContent).toContain('Tasks');
		expect(page.textContent).toContain('$ per accepted task');
		expect(page.textContent).toContain('Share of spend');
		expect(page.textContent).not.toContain('conversation');
	});
	const served = [
		...Array.from({ length: 6 }, (_, index) => ({ model: `model-${index}`, label: `model-${index}`, share_of_tokens: .1, tokens: 526_000_000 })),
		{ model: '', label: 'Other (3 more models)', share_of_tokens: .3, tokens: 9_800_000_000 },
	];
	it('labels every desktop pie slice the backend serves, its remainder included', () => {
		const page = render({ tokenShares: served });
		expect(page.querySelector('.pie')).not.toBeNull();
		expect(page.textContent).toContain('Other (3 more models)');
		expect(page.textContent).toContain('30.0%');
	});
	it('renders the token shares it is given and never regroups them', () => {
		const page = render({ tokenShares: Array.from({ length: 9 }, (_, index) => ({ model: `model-${index}`, label: `model-${index}`, share_of_tokens: .1, tokens: 526_000_000 })) });
		expect(page.querySelectorAll('.legend li')).toHaveLength(9);
		expect(page.textContent).toContain('model-8');
	});
	it('gives every slice, the remainder included, its own color and a visible value', () => {
		const page = render({ tokenShares: served });
		const colors = [...page.querySelectorAll<HTMLElement>('.legend i')].map((element) => element.style.background);
		expect(colors).toHaveLength(7);
		expect(new Set(colors).size).toBe(7);
		expect(page.querySelectorAll('.legend b')).toHaveLength(7);
	});
	it('shows each slice\'s token total beside its share', () => {
		const items = [...render({ tokenShares: served }).querySelectorAll('.legend li')].map((item) => item.textContent);
		expect(items[0]).toContain('10.0%');
		expect(items[0]).toContain('526M');
	});
	it('captions the pie as a share of tokens by model for the latest window, above it, at every width', () => {
		const span = { from: '2026-09-28', to: '2026-10-04' };
		const page = render({ period: { grain: 'days', timezone: 'UTC', current: '2026-10-05', range: span, latest: span, previous: { from: '2026-09-21', to: '2026-09-27' } } });
		const share = page.querySelector('.share');
		const caption = share?.firstElementChild;
		expect(caption?.textContent).toBe('Share of tokens by model, Sep 28 to Oct 4');
		expect(caption?.compareDocumentPosition(share!.querySelector('.pie')!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
		expect(share?.getAttribute('aria-label')).toBe('Token share by model');
	});
	it('marks unavailable models in text', () => {
		const page = render({ rows: [{ ...row, model: 'codex/gpt-5.6-sol', label: 'gpt-5.6-sol', not_offered_now: true }] });
		expect(page.textContent).toContain('not offered now');
	});
	it('says $ per accepted task counts the whole chain, in API-equivalent dollars', () => {
		expect(render().textContent).toContain('$ per accepted task, whole chain, API-equivalent.');
	});
	/** The rate cell of one row rendered alone. */
	function rateCell(r: Partial<ModelsState['rows'][number]>): string {
		const page = render({ rows: [{ ...row, ...r }] });
		const cell = page.querySelector('tbody tr td.blended')?.textContent ?? '';
		host?.remove(); component && unmount(component); component = null;
		return cell;
	}
	it('shows only the model, its tasks and its rate: failed turns and the change column are gone', () => {
		const page = render({ rows: [{ ...row, failed_turns_per_100: null }] });
		expect([...page.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Model', 'Share of spend', '$ per accepted task', 'Tasks']);
		expect(page.textContent).not.toMatch(/failed turns|too few turns|vs last week|-13%|too few before/i);
	});
	it('marks a row with no priced spend so a phone drops its Share pair', () => {
		const page = render({ rows: [row, other] });
		expect([...page.querySelectorAll('tbody td.spend')].map((td) => td.classList.contains('nospend'))).toEqual([false, true]);
	});
	it('names the latest window in the Models heading too', () => {
		const span = { from: '2026-09-28', to: '2026-10-04' };
		const page = render({ period: { grain: 'days', timezone: 'UTC', current: '2026-10-05', range: span, latest: span, previous: { from: '2026-09-21', to: '2026-09-27' } } });
		expect(page.querySelector('#models-title')?.textContent).toBe('Models, Sep 28 to Oct 4');
	});
	it('shows each row its share of the window\'s whole spend, a dash where nothing was priced', () => {
		// $4,581.6 is on the rows; the other $418.4 of the window\'s ledger belongs to models with no row of their own.
		const page = render({ rows: [row, { ...row, model: 'b', label: 'B', spend_usd: 3436.2 }, other], totalSpendUsd: 5000 });
		const cells = [...page.querySelectorAll('tbody tr td.spend')].map((td) => td.textContent);
		expect(cells).toEqual(['22.9%', '68.7%', '—']);
	});
	it('shows no share while the window has no priced spend', () => {
		const page = render({ totalSpendUsd: 0 });
		expect([...page.querySelectorAll('tbody tr td.spend')].map((td) => td.textContent)).toEqual(['—']);
	});
	it('names the cause in the rate cell when there is no figure', () => {
		expect(rateCell({})).toBe('$2.30');
		expect(rateCell({ recorded_cost_per_task: null, cost_class: 'no_pricing', rate_reason: 'unpriced' })).toBe('unpriced');
		expect(rateCell({ recorded_cost_per_task: null, cost_class: 'local', rate_reason: 'local' })).toBe('no list price');
		expect(rateCell({ tasks: 0, recorded_cost_per_task: null, cost_class: null, rate_reason: 'no_tasks' })).toBe('—');
	});
	it('groups the models past the named rows as Other and keeps their unknown cost unknown', () => {
		const page = render({ rows: [row, other] });
		const cells = [...page.querySelectorAll('tbody tr')].at(-1)?.textContent ?? '';
		expect(cells).toContain('Other (3 more models)');
		expect(cells).toContain('unpriced');
		expect(cells).not.toContain('$0');
		expect(page.textContent).not.toContain('share the last table row');
	});
	it('discloses local models in a priced group without calling its calls unpriced', () => {
		const page = render({ rows: [{ ...other, recorded_cost_per_task: 2, cost_class: 'priced', rate_reason: 'none', includes_local: true }] });
		const cells = page.querySelector('tbody tr')?.textContent ?? '';
		expect(cells).toContain('includes local models with no list price');
		expect(cells).toContain('$2.00');
		expect(cells).not.toContain('*');
		expect(page.textContent).not.toContain('Some calls were unpriced');
	});
	it('marks a rate with a recording gap as a lower bound and says why', () => {
		const page = render({ rows: [{ ...row, recording_gap: true }] });
		expect(page.querySelector('tbody tr')?.textContent).toContain('at least $2.30');
		expect(page.textContent).toContain('a call failed to record, so the figure may be low');
	});
	it('shows a partly priced rate with one marker and one note, never as unpriced', () => {
		const page = render({ rows: [{ ...row, cost_class: 'no_pricing' }, { ...row, model: 'codex/gpt-6.1-sol', label: 'GPT-6.1 Sol', recorded_cost_per_task: 0.64, cost_class: 'no_pricing' }] });
		const cells = [...page.querySelectorAll('tbody tr')].map((tr) => tr.textContent ?? '');
		expect(cells[0]).toContain('$2.30*');
		expect(cells[1]).toContain('$0.64*');
		expect(page.textContent).not.toContain('unpriced*');
		expect(page.querySelectorAll('.note')).toHaveLength(3);
		expect(page.textContent?.match(/\* Some calls were unpriced; the figure covers priced calls only\./g)).toHaveLength(1);
		host?.remove(); component && unmount(component); component = null;
		const complete = render();
		expect(complete.textContent).toContain('$2.30');
		expect(complete.textContent).not.toContain('*');
	});
	it('discloses the unresolved share, and never failed turns', () => {
		const page = render({ unmatchedFailedTurns: 3 });
		expect(page.textContent).toContain('2.6% of tokens ran under a model alias no record resolves');
		expect(page.textContent).not.toContain('failed turns');
	});
	it('keeps load, empty, and recoverable error states', () => {
		expect(render({ rows: [], tokenShares: [], loading: true }).querySelector('.skeleton')).not.toBeNull();
		host?.remove(); component && unmount(component); component = null;
		expect(render({ rows: [], tokenShares: [] }).textContent).toContain('No model activity in this scope.');
		host?.remove(); component && unmount(component); component = null;
		expect(render({ rows: [], tokenShares: [], error: 'Could not load models.' }).querySelector('button')?.textContent).toBe('Retry');
	});
	const cell = { size: 'M' as const, tasks: 20, cost_per_task: 3.09, cost_change: -0.07, change_reason: 'none' as const, first_try: 0.92, partial: false, recording_gap: false };
	it('replaces the blended rate with one column per size class that any row has', () => {
		const sized = { ...row, sizes: [{ ...cell, size: 'S' as const, cost_per_task: 1.07, change_reason: 'new' as const, cost_change: null, first_try: 1 }, cell] };
		const page = render({ rows: [sized, { ...row, model: 'b', label: 'B', sizes: [{ ...cell, size: 'L' as const, cost_per_task: 8.56, cost_change: 0.24 }] }] });
		expect([...page.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Model', 'Share of spend', '$ per task, by size class', 'Tasks', 'S', 'M', 'L']);
		expect(page.querySelector('thead th.by-size')?.getAttribute('colspan')).toBe('3');
		const [first, second] = [...page.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td.size')].map((td) => td.textContent?.replace(/\s+/g, ' ').trim()));
		expect(first).toEqual(['S $1.07 new · 100% first try', 'M $3.09 -7% · 92% first try', '—']);
		expect(second).toEqual(['—', '—', 'L $8.56 +24% · 92% first try']);
		expect(page.querySelector('td.blended')).toBeNull();
		expect(page.textContent).toContain('$ per task within each size class, whole chain, API-equivalent; a class shows only with 14 or more tasks.');
		expect(page.textContent).toContain('A change compares one named size class that has 14 or more tasks in both windows, otherwise n/a, mix changed.');
	});
	it('shows the blended rate across the size columns for a row with no size cell', () => {
		const page = render({ rows: [{ ...row, sizes: [cell] }, { ...row, model: 'b', label: 'B', recorded_cost_per_task: 0.5 }, { ...row, model: 'c', label: 'C', recorded_cost_per_task: null, cost_class: 'local', rate_reason: 'local' }] });
		const spans = [...page.querySelectorAll<HTMLTableCellElement>('td.blended')];
		expect(spans.map((td) => td.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['$0.50 per accepted task, all sizes', 'no list price']);
		expect(spans[0].colSpan).toBe(1);
	});
	it('marks a size cell that is a lower bound and footnotes it once', () => {
		const page = render({ rows: [{ ...row, sizes: [{ ...cell, partial: true, recording_gap: true }] }] });
		expect(page.querySelector('td.size')?.textContent).toContain('at least $3.09*');
		expect(page.textContent).toContain('Some calls were unpriced');
		expect(page.textContent).toContain('a call failed to record');
	});
	it('hides an Other row with no tasks and its sentence, and shows one with tasks', () => {
		const empty = render({ rows: [row, { ...other, tasks: 0, rate_reason: 'no_tasks' }] });
		expect(empty.querySelectorAll('tbody tr')).toHaveLength(1);
		expect(empty.textContent).not.toContain('Other models');
		expect(empty.textContent).not.toContain('share the last table row');
		host?.remove(); component && unmount(component); component = null;
		const shown = render({ rows: [row, other] });
		expect(shown.querySelectorAll('tbody tr')).toHaveLength(2);
		expect(shown.textContent).toContain('Other (3 more models)');
	});
});
