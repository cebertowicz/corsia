import { describe, expect, it } from 'vitest';
import { countUp } from './percentCountUp';
import { CREEP_STALE_MS, PHASE_SECONDS, ProgressModel, remoteCeiling, remoteEstimate, type StepId } from './screenProgress';

type Leg = { step: StepId; seconds: number; drainCap?: number; count?: (t: number) => { done: number; total: number } };

/** Replays a timeline at one tick a second and counts the shown number up at 20 frames a second, as the screen does. */
function replay(legs: Leg[]) {
	const model = new ProgressModel();
	const boundaries: { at: number; step: StepId; percent: number }[] = [];
	const perSecond: number[] = [];
	let shown = 0;
	let at = 0;
	const total = legs.reduce((sum, leg) => sum + leg.seconds, 0);
	for (const leg of legs) {
		for (let t = 0; t < leg.seconds; t += 1) {
			model.update(at * 1000, { step: leg.step, count: leg.count?.(t) ?? null, alive: true, drainRemaining: leg.drainCap === undefined ? null : Math.max(0, leg.drainCap - t) });
			if (t === 0) boundaries.push({ at, step: leg.step, percent: model.percent });
			const before = shown;
			for (let frame = 0; frame < 20; frame += 1) shown = countUp(shown, model.percent, 50);
			perSecond.push(shown - before);
			at += 1;
		}
	}
	return { model, boundaries, perSecond, total };
}

const NO_MIGRATION: Leg[] = [{ step: 'restarting', seconds: 14 }, { step: 'starting', seconds: 17 }, { step: 'verifying', seconds: 4 }];

describe('update screen percentage replays', () => {
	it('(a) a no-migration update with the drain held to its 60 s cap tracks elapsed time at every phase boundary', () => {
		const { boundaries, perSecond, total, model } = replay([{ step: 'waiting', seconds: 60, drainCap: 60 }, ...NO_MIGRATION]);
		for (const boundary of boundaries) expect(Math.abs(boundary.percent - (boundary.at / total) * 100), `${boundary.step} at ${boundary.at}s`).toBeLessThanOrEqual(10);
		expect(Math.max(...perSecond)).toBeLessThanOrEqual(15);
		expect(model.percent).toBeLessThan(100);
	});

	it('(b) the same update with the drain ending in 2 s tracks elapsed time too', () => {
		const { boundaries, perSecond, total } = replay([{ step: 'waiting', seconds: 2, drainCap: 60 }, ...NO_MIGRATION]);
		for (const boundary of boundaries) expect(Math.abs(boundary.percent - (boundary.at / total) * 100), `${boundary.step} at ${boundary.at}s`).toBeLessThanOrEqual(10);
		expect(Math.max(...perSecond)).toBeLessThanOrEqual(15);
	});

	it('(c) a migration update shaped like the 24 GB replay never goes back or jumps once the data steps show', () => {
		const { perSecond, model } = replay([
			{ step: 'waiting', seconds: 60, drainCap: 60 },
			{ step: 'restarting', seconds: 14 },
			{ step: 'backing_up', seconds: PHASE_SECONDS.backing_up, count: (t) => ({ done: t, total: PHASE_SECONDS.backing_up }) },
			{ step: 'upgrading', seconds: PHASE_SECONDS.upgrading, count: (t) => ({ done: t, total: PHASE_SECONDS.upgrading }) },
			{ step: 'checking_data', seconds: PHASE_SECONDS.checking_data },
			{ step: 'starting', seconds: 17 },
			{ step: 'verifying', seconds: 4 },
		]);
		expect(Math.min(...perSecond)).toBeGreaterThanOrEqual(0);
		expect(Math.max(...perSecond)).toBeLessThanOrEqual(15);
		expect(model.percent).toBeLessThan(100);
	});

	it('never falls when the data steps show late, and spends the rest of the distance on what is left', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'restarting', count: null, alive: true });
		model.update(14_000, { step: 'restarting', count: null, alive: true });
		const before = model.percent;
		model.update(15_000, { step: 'backing_up', count: null, alive: true });
		expect(model.percent).toBeGreaterThanOrEqual(before);
		expect(model.percent - before).toBeLessThan(2);
	});

	it('moves a drain by the higher of its agent count and its time', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'waiting', count: { done: 0, total: 3 }, alive: true, drainRemaining: 60 });
		model.update(1_000, { step: 'waiting', count: { done: 2, total: 3 }, alive: true, drainRemaining: 59 });
		const byCount = model.percent;
		const timeOnly = new ProgressModel();
		timeOnly.update(0, { step: 'waiting', count: null, alive: true, drainRemaining: 60 });
		timeOnly.update(1_000, { step: 'waiting', count: null, alive: true, drainRemaining: 59 });
		expect(byCount).toBeGreaterThan(timeOnly.percent);
	});

	it('stops the clock when the source is not alive, and never goes back', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'restarting', count: null, alive: true });
		model.update(5_000, { step: 'restarting', count: null, alive: true });
		const moving = model.percent;
		model.update(5_000 + CREEP_STALE_MS, { step: 'restarting', count: null, alive: false });
		expect(model.percent).toBe(moving);
		model.update(30_000, { step: 'waiting', count: { done: 1, total: 1 }, alive: true });
		expect(model.step).toBe('restarting');
		expect(model.percent).toBeGreaterThanOrEqual(moving);
	});

	it('holds short of a phase end until the phase really ends, and short of 100 always', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'verifying', count: null, alive: true });
		model.update(600_000, { step: 'verifying', count: null, alive: true });
		expect(model.percent).toBeLessThan(100);
	});

	it('restores a snapshot, including one from a version that wrote no phase, without resetting', () => {
		const model = new ProgressModel();
		model.update(0, { step: 'restarting', count: null, alive: true });
		model.update(7_000, { step: 'restarting', count: null, alive: true });
		const restored = ProgressModel.restore(JSON.parse(JSON.stringify(model.snapshot())));
		expect(restored.percent).toBe(model.percent);
		const old = ProgressModel.restore({ ...model.snapshot(), phase: null });
		old.update(8_000, { step: 'restarting', count: null, alive: true });
		expect(old.percent).toBeGreaterThanOrEqual(model.percent);
	});

	it('counts the remote estimate up to the restart share and no further', () => {
		expect(remoteEstimate(15, 60_000, 30)).toBe(remoteCeiling(15));
		expect(remoteEstimate(15, 15_000, 30)).toBeGreaterThan(15);
		expect(remoteEstimate(15, 15_000, 30)).toBeLessThan(remoteCeiling(15));
		expect(remoteCeiling(15)).toBeLessThan(99);
	});
});
