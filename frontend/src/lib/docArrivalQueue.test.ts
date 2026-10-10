import { describe, expect, it, vi } from 'vitest';
import { ArrivalQueue, REDUCED_HIGHLIGHT_FOR, revealDelay } from './docArrivalQueue';
import type { SelectOutcome } from './cm/typed-reveal-arrival';

interface Setup {
	events: string[];
	viewing: boolean;
	reduced: boolean;
	selects: Array<(outcome: SelectOutcome) => void>;
	canSelect: boolean;
	revealFails: boolean;
	sleeps: number[];
}

function queue(overrides: Partial<Setup> = {}) {
	const state: Setup = { events: [], viewing: true, reduced: false, selects: [], canSelect: true, revealFails: false, sleeps: [], ...overrides };
	const q = new ArrivalQueue<string>({
		isViewing: () => state.viewing,
		reducedMotion: () => state.reduced,
		prepare: async (value) => ({ value, chars: 100, delayMs: 3_000 }),
		select: (value) => {
			if (!state.canSelect) return null;
			state.events.push(`select:${value}`);
			return new Promise<SelectOutcome>((resolve) => { state.selects.push(resolve); });
		},
		highlight: (value, selecting) => { state.events.push(`highlight:${value}:${selecting ? 'select' : 'plain'}`); },
		reveal: async (value) => { state.events.push(`reveal:${value}`); if (state.revealFails) throw new Error('reveal failed'); },
		clear: (value) => { state.events.push(`clear:${value}`); },
		sleep: async (ms) => { state.sleeps.push(ms); },
	});
	return { q, state };
}
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe('arrival queue awaits the editor select instead of running a second hold clock', () => {
	it.each<SelectOutcome>(['ended', 'interrupted', 'unavailable'])('reveals and clears once the select settles %s, with no queue delay', async (outcome) => {
		const { q, state } = queue();
		q.enqueue('c1');
		const done = q.run();
		await tick();
		expect(state.events).toEqual(['select:c1', 'highlight:c1:select']);
		state.selects[0](outcome);
		await done;
		expect(state.events).toEqual(['select:c1', 'highlight:c1:select', 'reveal:c1', 'clear:c1']);
		expect(state.sleeps).toEqual([]);
	});

	it('uses its own delay, once per arrival, when no editor can run the select', async () => {
		const { q, state } = queue({ canSelect: false });
		q.enqueue('c1');
		await q.run();
		expect(state.events).toEqual(['highlight:c1:plain', 'reveal:c1', 'clear:c1']);
		expect(state.sleeps).toEqual([3_000]);
	});

	it('falls back to the default delay scaled by the text when no delay was prepared', async () => {
		const state: Setup = { events: [], viewing: true, reduced: false, selects: [], canSelect: false, revealFails: false, sleeps: [] };
		const q = new ArrivalQueue<string>({
			isViewing: () => true, reducedMotion: () => false, prepare: async (value) => ({ value, chars: 40 }),
			highlight: () => {}, reveal: async () => {}, clear: () => {}, sleep: async (ms) => { state.sleeps.push(ms); },
		});
		q.enqueue('c1');
		await q.run();
		expect(state.sleeps).toEqual([revealDelay(40)]);
	});

	it('under reduced motion asks for no select: card at once, short highlight, then clear', async () => {
		const { q, state } = queue({ reduced: true });
		q.enqueue('c1');
		await q.run();
		expect(state.events).toEqual(['highlight:c1:plain', 'reveal:c1', 'clear:c1']);
		expect(state.sleeps).toEqual([REDUCED_HIGHLIGHT_FOR]);
	});

	it('clears even when reveal throws, and the failure still surfaces', async () => {
		const { q, state } = queue({ revealFails: true });
		q.enqueue('c1');
		const done = q.run();
		await tick();
		state.selects[0]('ended');
		await expect(done).rejects.toThrow('reveal failed');
		expect(state.events.slice(-2)).toEqual(['reveal:c1', 'clear:c1']);
	});

	it('pauses and requeues the same arrival when the view stopped showing while it ran, then shows it at once on wake', async () => {
		const { q, state } = queue();
		q.enqueue('c1');
		const done = q.run();
		await tick();
		state.viewing = false;
		state.selects[0]('unavailable');
		await done;
		expect(state.events).toEqual(['select:c1', 'highlight:c1:select', 'clear:c1']);
		expect(q.pending).toBe(1);
		state.viewing = true;
		const woke = q.wake();
		await tick();
		await woke;
		expect(state.selects).toHaveLength(1);
		expect(state.events).toContain('reveal:c1');
	});
});

function laneQueue(viewing: { on: boolean }, events: string[], waits: Array<() => void>) {
	return new ArrivalQueue<string>({
		isViewing: () => viewing.on,
		reducedMotion: () => false,
		prepare: async (value) => ({ value, chars: 10, delayMs: 5_000 }),
		highlight: (value) => { events.push('highlight:' + value); },
		reveal: (value) => { events.push('reveal:' + value); },
		clear: (value) => { events.push('clear:' + value); },
		paused: (waiting) => { events.push('paused:' + waiting.join(',')); },
		sleep: () => new Promise<void>((resolve) => { waits.push(resolve); }),
	});
}

describe('arrival queue lanes', () => {
	it('reports every waiting arrival when the viewer is gone before the next turn', async () => {
		const viewing = { on: false };
		const events: string[] = [];
		const q = laneQueue(viewing, events, []);
		q.enqueue('a'); q.enqueue('b');
		await q.run();
		expect(events).toEqual(['paused:a,b']);
	});

	it('reports the arrival whose wait ended with nobody viewing, with the ones behind it', async () => {
		const viewing = { on: true };
		const events: string[] = [];
		const waits: Array<() => void> = [];
		const q = laneQueue(viewing, events, waits);
		q.enqueue('a'); q.enqueue('b');
		const done = q.run();
		await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
		viewing.on = false;
		waits[0]();
		await done;
		expect(events).toEqual(['highlight:a', 'clear:a', 'paused:a,b']);
	});

	it('revealNow ends the wait and shows the arrival at once', async () => {
		const events: string[] = [];
		const waits: Array<() => void> = [];
		const q = laneQueue({ on: true }, events, waits);
		q.enqueue('a');
		const done = q.run();
		await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
		q.revealNow();
		await done;
		expect(events).toEqual(['highlight:a', 'reveal:a', 'clear:a']);
	});

	it('revealNow also ends the editor select clock, so a click shows the arrival at once', async () => {
		const { q, state } = queue();
		q.enqueue('c1');
		const done = q.run();
		await tick();
		q.revealNow();
		await done;
		expect(state.events).toEqual(['select:c1', 'highlight:c1:select', 'reveal:c1', 'clear:c1']);
	});
});
