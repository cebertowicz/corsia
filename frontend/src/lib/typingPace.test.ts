import { describe, expect, it } from 'vitest';
import {
	hashSeed, keystrokeFactor, mulberry32, paceUnit, shownAt, stepTimes, typingDuration, typingSchedule,
} from './typingPace';

const WORDS = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa'];

function prose(chars: number): string {
	let out = '';
	let i = 0;
	while (out.length < chars) {
		out += WORDS[i % WORDS.length] + (i % 9 === 8 ? '. ' : i % 5 === 4 ? ', ' : ' ');
		i += 1;
	}
	return out.slice(0, chars);
}

const sum = (steps: { delayMs: number }[]) => steps.reduce((a, s) => a + s.delayMs, 0);

describe('typing duration', () => {
	it.each([
		[10, 1_240],
		[100, 4_400],
		[500, 10_700],
		[3_000, 12_000],
		[20_000, 12_000],
	])('%i characters take about %i ms', (chars, expected) => {
		const schedule = typingSchedule(prose(chars), 7);
		expect(schedule.totalMs / expected).toBeGreaterThan(0.85);
		expect(schedule.totalMs / expected).toBeLessThan(1.15);
		expect(sum(schedule.steps) / schedule.totalMs).toBeGreaterThan(0.85);
		expect(sum(schedule.steps) / schedule.totalMs).toBeLessThan(1.15);
	});

	it('a 3000 character backlog finishes within 12 s with no step longer than a short phrase', () => {
		const schedule = typingSchedule(prose(3_000), 7);
		expect(schedule.totalMs).toBeLessThanOrEqual(12_000 * 1.15);
		expect(Math.max(...schedule.steps.map((step) => step.chars))).toBeLessThanOrEqual(80);
	});

	it('floors short text and caps long text', () => {
		expect(typingDuration(1)).toBe(400);
		expect(typingDuration(1_000_000)).toBe(12_000);
		expect(typingDuration(0)).toBe(0);
	});
});

describe('typing unit', () => {
	it('switches at the rate thresholds', () => {
		expect(paceUnit(39.9)).toBe('char');
		expect(paceUnit(40)).toBe('word');
		expect(paceUnit(150)).toBe('word');
		expect(paceUnit(150.1)).toBe('phrase');
	});

	it('picks the unit from the schedule rate', () => {
		expect(typingSchedule(prose(100), 1).unit).toBe('char');
		expect(typingSchedule(prose(500), 1).unit).toBe('word');
		expect(typingSchedule(prose(20_000), 1).unit).toBe('phrase');
	});

	it('reveals every character exactly once', () => {
		for (const chars of [10, 100, 500, 3_000]) {
			const text = prose(chars);
			expect(typingSchedule(text, 3).steps.reduce((a, s) => a + s.chars, 0)).toBe(text.length);
		}
	});

	it('never splits a surrogate pair in character mode', () => {
		const text = 'ab \u{1F600} cd \u{1F680}\u{1F680} ef gh ij kl mn op';
		const schedule = typingSchedule(text, 5);
		expect(schedule.unit).toBe('char');
		let cut = 0;
		for (const step of schedule.steps) {
			cut += step.chars;
			const code = text.charCodeAt(cut - 1);
			expect(code >= 0xd800 && code <= 0xdbff).toBe(false);
		}
	});

	it('never ends a word step mid-word', () => {
		const text = prose(500);
		let cut = 0;
		for (const step of typingSchedule(text, 5).steps) {
			cut += step.chars;
			if (cut < text.length) expect(/\s/.test(text[cut - 1]) || /\s/.test(text[cut])).toBe(true);
		}
	});

	it('keeps phrase steps within a line', () => {
		const text = prose(20_000).replace(/\. /g, '.\n');
		const steps = typingSchedule(text, 5).steps;
		let at = 0;
		for (const step of steps) {
			const piece = text.slice(at, at + step.chars);
			expect(piece.trimEnd().includes('\n')).toBe(false);
			expect(step.chars).toBeLessThanOrEqual(120);
			at += step.chars;
		}
	});
});

describe('typing rhythm', () => {
	it('pauses longer after sentence ends than inside a sentence', () => {
		const text = prose(500);
		const steps = typingSchedule(text, 11).steps;
		let at = 0;
		const rate = { sentence: [] as number[], comma: [] as number[], plain: [] as number[] };
		for (const step of steps) {
			const piece = text.slice(at, at + step.chars).trimEnd();
			at += step.chars;
			const perChar = step.delayMs / step.chars;
			if (/[.!?]$/.test(piece)) rate.sentence.push(perChar);
			else if (/[,;:]$/.test(piece)) rate.comma.push(perChar);
			else rate.plain.push(perChar);
		}
		const mean = (values: number[]) => values.reduce((a, b) => a + b, 0) / values.length;
		expect(mean(rate.sentence)).toBeGreaterThan(mean(rate.comma));
		expect(mean(rate.comma)).toBeGreaterThan(mean(rate.plain));
	});

	it('pauses at a paragraph break', () => {
		const text = 'First line here.\n\nSecond line here.';
		const steps = typingSchedule(text, 2).steps;
		let at = 0;
		const delays = steps.map((step) => {
			const piece = text.slice(at, at + step.chars);
			at += step.chars;
			return { piece, delayMs: step.delayMs };
		});
		const breakStep = delays.filter((d) => d.piece === '\n')[1];
		const others = delays.filter((d) => d.piece !== '\n').map((d) => d.delayMs).sort((a, b) => a - b);
		expect(breakStep.delayMs).toBeGreaterThan(others[Math.floor(others.length / 2)] * 3);
	});

	it('jitters step delays', () => {
		const delays = typingSchedule(prose(100), 4).steps
			.filter((_, i) => i % 7 === 0).map((s) => s.delayMs);
		expect(new Set(delays.map((d) => d.toFixed(3))).size).toBeGreaterThan(5);
	});

	it('is deterministic per seed', () => {
		const text = prose(300);
		expect(typingSchedule(text, 9)).toEqual(typingSchedule(text, 9));
		expect(typingSchedule(text, 9)).not.toEqual(typingSchedule(text, 10));
		expect(hashSeed('thread:2026')).toBe(hashSeed('thread:2026'));
		expect(hashSeed('a')).not.toBe(hashSeed('b'));
		expect(mulberry32(1)()).toBe(mulberry32(1)());
	});
});

describe('reveal clock', () => {
	it('shows the first step at once and the rest on schedule', () => {
		const steps = [{ chars: 2, delayMs: 100 }, { chars: 3, delayMs: 50 }, { chars: 1, delayMs: 10 }];
		const times = stepTimes(steps);
		expect(times).toEqual([0, 100, 150]);
		expect(shownAt(steps, times, 0)).toBe(2);
		expect(shownAt(steps, times, 99)).toBe(2);
		expect(shownAt(steps, times, 100)).toBe(5);
		expect(shownAt(steps, times, 150)).toBe(6);
	});
});

describe('keystroke jitter', () => {
	const plain = (chars: number) => WORDS.join(' ').repeat(Math.ceil(chars / 40)).slice(0, chars);
	const cv = (values: number[]) => {
		const mean = values.reduce((a, b) => a + b, 0) / values.length;
		return Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length) / mean;
	};

	it('spreads inter-keystroke delays widely (mean coefficient of variation above 0.6)', () => {
		const spreads = Array.from({ length: 40 }, (_, seed) => {
			const schedule = typingSchedule(plain(100), seed + 1);
			expect(schedule.unit).toBe('char');
			return cv(schedule.steps.map((step) => step.delayMs));
		});
		expect(spreads.reduce((a, b) => a + b, 0) / spreads.length).toBeGreaterThan(0.6);
	});

	it('keeps each multiplier within 0.35x to 2.2x and most near 1x', () => {
		const random = mulberry32(11);
		const factors = Array.from({ length: 2_000 }, () => keystrokeFactor(random));
		expect(Math.min(...factors)).toBeGreaterThanOrEqual(0.35);
		expect(Math.max(...factors)).toBeLessThanOrEqual(2.2);
		expect(factors.filter((f) => f >= 0.8 && f <= 1.2).length / factors.length).toBeGreaterThan(0.6);
	});

	it('redistributes time without moving the total past the cap', () => {
		for (const chars of [40, 100, 500, 20_000]) {
			for (let seed = 1; seed <= 10; seed++) {
				const schedule = typingSchedule(plain(chars), seed);
				expect(sum(schedule.steps)).toBeCloseTo(schedule.totalMs, 3);
				expect(schedule.totalMs).toBeLessThanOrEqual(12_000);
			}
		}
	});
});

describe('select-hold sequence timing', () => {
	it('sweeps 200 ms + 6 ms per character, capped at 700 ms', async () => {
		const { selectSweepMs } = await import('./typingPace');
		expect(selectSweepMs(0)).toBe(200);
		expect(selectSweepMs(10)).toBe(260);
		expect(selectSweepMs(1_000)).toBe(700);
	});

	it('holds a comment 600 ms + 15 ms per body character, capped at 5 s', async () => {
		const { commentHoldMs, commentArrivalDelay } = await import('./typingPace');
		expect(commentHoldMs(0)).toBe(600);
		expect(commentHoldMs(100)).toBe(2_100);
		expect(commentHoldMs(10_000)).toBe(5_000);
		expect(commentArrivalDelay(10, 100)).toBe(260 + 2_100);
	});

	it('holds replaced text 400-600 ms', async () => {
		const { ghostHoldMs } = await import('./typingPace');
		expect(ghostHoldMs(() => 0)).toBe(400);
		expect(ghostHoldMs(() => 1)).toBe(600);
	});
});
