import { describe, expect, it } from 'vitest';
import { CREEP_STALE_MS, ProgressModel, remoteEstimate } from './screenProgress';

describe('update screen progress', () => {
	it('never falls, limits creep to a live band, and reserves 100 for confirmation', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'waiting', count: null, alive: true });
		const early = model.percent;
		model.update(2_000, { step: 'waiting', count: null, alive: true });
		const moving = model.percent;
		model.update(2_000 + CREEP_STALE_MS, { step: 'waiting', count: null, alive: false });
		const frozen = model.percent;
		model.update(20_000, { step: 'restarting', count: null, alive: true });
		expect(moving).toBeGreaterThan(early);
		expect(frozen).toBe(moving);
		expect(model.percent).toBeGreaterThanOrEqual(frozen);
		expect(model.percent).toBeLessThan(100);
	});

	it('uses counts only inside their band and ignores a regressing phase', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'backing_up', count: { done: 3, total: 4 }, alive: true });
		const backup = model.percent;
		model.update(1_000, { step: 'waiting', count: { done: 1, total: 1 }, alive: true });
		expect(model.step).toBe('backing_up');
		expect(model.percent).toBeGreaterThanOrEqual(backup);
	});

	it('caps a remote estimate below completion', () => {
		expect(remoteEstimate(40, 60_000, 30)).toBe(95);
		expect(remoteEstimate(40, 15_000, 30)).toBeGreaterThan(40);
	});
});
