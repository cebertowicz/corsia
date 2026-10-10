import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { decodeModelsResponse, loadMeasurementModels, measurementModels } from './measurementModels';
import { measurementScope } from './measurement';
import { measurementGrain } from './measurementGrain';

const payload = { rows: [{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', tasks: 14, failed_turns_per_100: 2.7, recorded_cost_per_task: 2.3, spend_usd: 32.2, cost_class: 'priced' as const, rate_reason: 'none' as const, cost_change: null, change_reason: 'too_few_before' as const, previous_tasks: 12, not_offered_now: false, other: false }], min_tasks: 14, token_shares: [{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', share_of_tokens: .8, tokens: 800 }], unmatched_failed_turns: 0, unresolved_token_share: .02 };

afterEach(() => { vi.unstubAllGlobals(); measurementGrain.set('days'); measurementScope.set({ project: 'all', agent: null }); measurementModels.set({ loading: false, error: null, rows: [], minTasks: null, tokenShares: [], unmatchedFailedTurns: 0, unresolvedTokenShare: 0, period: null, scopeKey: null, grain: null }); });

describe('measurement Models store', () => {
	it('decodes the resolved-model response and rejects raw-contract omissions', () => {
		expect(decodeModelsResponse(payload).rows[0]).toMatchObject({ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', cost_class: 'priced' });
		expect(() => decodeModelsResponse({ rows: [] })).toThrow('models response carried no rows');
		expect(() => decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], label: undefined }] })).toThrow('models row carried invalid model');
		expect(() => decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], cost_class: 'free' }] })).toThrow('models row carried invalid cost class');
		expect(decodeModelsResponse(payload).rows[0]).toMatchObject({ rate_reason: 'none', change_reason: 'too_few_before' });
		for (const reasons of [{ rate_reason: undefined }, { rate_reason: 'zero' }, { change_reason: undefined }, { change_reason: 'not_recorded' }]) {
			expect(() => decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], ...reasons }] })).toThrow('models row carried invalid cost reason');
		}
	});
	it('decodes spend and token totals, spend staying null when nothing was priced', () => {
		const decoded = decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], spend_usd: null }] });
		expect([decoded.rows[0].spend_usd, decoded.tokenShares[0].tokens, decoded.period]).toEqual([null, 800, null]);
		expect(decodeModelsResponse(payload).rows[0].spend_usd).toBe(32.2);
		expect(() => decodeModelsResponse({ ...payload, token_shares: [{ ...payload.token_shares[0], tokens: undefined }] })).toThrow('invalid count');
	});
	it('decodes the served minimum-tasks floor and the rate disclosures', () => {
		const decoded = decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], includes_local: true, recording_gap: true }] });
		expect(decoded.minTasks).toBe(14);
		expect(decoded.rows[0]).toMatchObject({ includes_local: true, recording_gap: true });
		expect(() => decodeModelsResponse({ ...payload, min_tasks: undefined })).toThrow('models response carried invalid count');
	});
	it('decodes size cells and rejects a bad one', () => {
		const cell = { size: 'M', tasks: 20, cost_per_task: 3.09, cost_change: -0.07, change_reason: 'none', first_try: 0.92, partial: false, recording_gap: true };
		expect(decodeModelsResponse(payload).rows[0].sizes).toEqual([]);
		expect(decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], sizes: [cell] }] }).rows[0].sizes).toEqual([cell]);
		for (const bad of [{ size: 'XXXL' }, { first_try: 1.2 }, { cost_per_task: null }, { change_reason: 'too_few_before' }, { cost_change: '7%' }, { tasks: -1 }]) {
			expect(() => decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], sizes: [{ ...cell, ...bad }] }] })).toThrow();
		}
		expect(() => decodeModelsResponse({ ...payload, rows: [{ ...payload.rows[0], sizes: {} }] })).toThrow('models row carried invalid size cells');
	});
	it('rejects a missing or invalid unresolved share like its sibling fields', () => {
		for (const value of [undefined, null, '0.1', -0.1, 1.5, Number.NaN]) {
			expect(() => decodeModelsResponse({ ...payload, unresolved_token_share: value })).toThrow('models response carried invalid unresolved token share');
		}
	});
	it('binds a successful response to its current scope and grain', async () => {
		const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => payload }); vi.stubGlobal('fetch', fetch);
		await loadMeasurementModels();
		expect(fetch.mock.calls[0][0]).toContain('project=all');
		expect(get(measurementModels)).toMatchObject({ scopeKey: 'all:none', grain: 'days', rows: payload.rows, tokenShares: payload.token_shares });
	});
	it('clears old bytes on a changed selection and gives a failed read a retryable sentence', async () => {
		measurementModels.set({ loading: false, error: null, rows: payload.rows.map((item) => ({ ...item, includes_local: false, recording_gap: false, sizes: [] })), minTasks: 14, tokenShares: payload.token_shares, unmatchedFailedTurns: 0, unresolvedTokenShare: .02, period: null, scopeKey: 'all:none', grain: 'days' });
		measurementScope.set({ project: 'p', agent: null }); vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }));
		await loadMeasurementModels();
		expect(get(measurementModels)).toMatchObject({ rows: [], tokenShares: [], scopeKey: null, error: 'Could not load models.' });
	});
});
