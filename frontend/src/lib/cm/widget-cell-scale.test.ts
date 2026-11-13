import { afterEach, describe, expect, it } from 'vitest';
import { build, cellEl, cellText, cleanup, clicked, typed } from './widget-cell-rig';
import type { Rig } from './typed-reveal-rig';

/**
 * A keystroke in an open cell costs work in proportion to the table's size, never to its size squared: typing in a cell of
 * a table twice as long does about twice the work. Work is counted, not timed: every predicate call the array searches
 * make during one keystroke (the cell lookups, the drawn-grid build, the patch that moves changed cells in).
 */

const rigs: Rig[] = [];
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; });

const tableOf = (rows: number) =>
	`# Plan\n\n| Feature | Status |\n| --- | --- |\n${Array.from({ length: rows }, (_, at) => `| Item ${at} | Open |`).join('\n')}\n\nafter\n`;

const SEARCHES = ['filter', 'find', 'findIndex', 'some', 'every'] as const;

/** The predicate calls `run` makes through the array searches. */
function predicateCalls(run: () => void): number {
	const proto = Array.prototype as unknown as Record<string, (...args: unknown[]) => unknown>;
	const saved = SEARCHES.map((name) => proto[name]);
	let calls = 0;
	SEARCHES.forEach((name, at) => {
		const original = saved[at];
		proto[name] = function (this: unknown[], ...args: unknown[]) {
			const [predicate, ...rest] = args as [(...args: unknown[]) => unknown, ...unknown[]];
			return original.call(this, (...inner: unknown[]) => { calls++; return predicate(...inner); }, ...rest);
		};
	});
	try { run(); } finally { SEARCHES.forEach((name, at) => { proto[name] = saved[at]; }); }
	return calls;
}

/** The work one keystroke does in an open cell halfway down a table of `rows` body rows. */
function keystrokeWork(rows: number): number {
	const r = build(tableOf(rows));
	rigs.push(r);
	clicked(r, cellEl(r, Math.floor(rows / 2), 0));
	expect(cellText(r), 'setup: the cell is open').toBe(`Item ${Math.floor(rows / 2) - 1}`);
	typed(r, 0, 'x'); // the first keystroke warms anything built once per open cell
	const work = predicateCalls(() => typed(r, 1, 'y'));
	expect(cellText(r)).toBe(`xyItem ${Math.floor(rows / 2) - 1}`);
	expect(r.view.state.doc.toString()).toContain(`| xyItem ${Math.floor(rows / 2) - 1} | Open |`);
	return work;
}

describe('typing in a cell of a long table', () => {
	it('does work linear in the table\'s size: 2,000 rows cost about twice what 1,000 do', () => {
		const half = keystrokeWork(1_000), full = keystrokeWork(2_000);
		expect(half, 'setup: the keystroke was counted').toBeGreaterThan(0);
		// Linear work doubles; rows x cells work quadruples.
		expect(full / half, `1,000 rows: ${half} calls, 2,000 rows: ${full}`).toBeLessThan(2.5);
	}, 120_000);
});
