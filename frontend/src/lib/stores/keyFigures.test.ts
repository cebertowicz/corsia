import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { measurementScope } from './measurement';
import { keyFigures, loadKeyFigures, type KpiPeriod } from './keyFigures';

const figure = (value: number) => ({ value, unknown_records: 0 });
const period = (total: number): KpiPeriod => ({ start: 's', end: 'e', from: '2026-09-28', to: '2026-10-04', tokens: { total: figure(total), input: figure(0), cache_write: figure(0), cache_read: figure(0), output: figure(0) } });
const body = (total: number) => ({ kpi: { current: period(total), previous: period(0) } });
const ok = (total: number) => ({ ok: true, json: async () => body(total) });
const ERROR = "Couldn't load today's figures.";
afterEach(() => { vi.unstubAllGlobals(); measurementScope.set({ project: 'all', agent: null }); });

describe('key figures store', () => {
	it('keeps a scope-keyed response and moves to a new scope', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(ok(1)).mockResolvedValueOnce(ok(2)));
		measurementScope.set({ project: 'project-a', agent: null });
		await loadKeyFigures({ project: 'project-a', agent: null });
		expect(get(keyFigures).kpi?.current.tokens.total.value).toBe(1);
		measurementScope.set({ project: 'project-b', agent: null });
		await loadKeyFigures({ project: 'project-b', agent: null });
		expect(get(keyFigures).kpi?.current.tokens.total.value).toBe(2);
		expect(get(keyFigures).selection).toContain('project-b');
	});

	it('rejects a response whose period carries no dates, never defaulting them', async () => {
		const { from: _from, ...undated } = period(1);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ kpi: { current: undated, previous: period(0) } }) }));
		await loadKeyFigures({ project: 'all', agent: null });
		expect(get(keyFigures).kpi).toBeNull();
		expect(get(keyFigures).error).toBe(ERROR);
	});

	it('reports a truthful retryable error', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
		await loadKeyFigures({ project: 'all', agent: null });
		expect(get(keyFigures).error).toBe(ERROR);
	});

	it('shows no values after a malformed load, never the previous response', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(ok(1)).mockResolvedValueOnce({ ok: true, json: async () => ({}) }));
		await loadKeyFigures({ project: 'all', agent: null });
		expect(get(keyFigures).kpi).not.toBeNull();
		await loadKeyFigures({ project: 'all', agent: null });
		expect(get(keyFigures).kpi).toBeNull();
		expect(get(keyFigures).error).toBe(ERROR);
	});

	it('ignores a response that arrives after a newer scope was asked for', async () => {
		let release: (value: unknown) => void = () => {};
		const slow = new Promise((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(slow).mockResolvedValueOnce(ok(9)));
		measurementScope.set({ project: 'old', agent: null });
		const first = loadKeyFigures({ project: 'old', agent: null });
		measurementScope.set({ project: 'new', agent: null });
		await loadKeyFigures({ project: 'new', agent: null });
		release(ok(1));
		await first;
		expect(get(keyFigures).kpi?.current.tokens.total.value).toBe(9);
		expect(get(keyFigures).selection).toContain('new');
	});
});
