/** Human-feeling typing schedule for text an agent has already written. */
export interface PaceStep {
	/** UTF-16 code units revealed by this step; never splits a surrogate pair. */
	chars: number;
	/** Wait after revealing this step, before the next one. */
	delayMs: number;
}

export type PaceUnit = 'char' | 'word' | 'phrase';

export interface PaceSchedule {
	steps: PaceStep[];
	totalMs: number;
	unit: PaceUnit;
}

export const PACE_MIN_MS = 400;
/** The longest any backlog may take; a bigger one types faster per character. */
export const PACE_MAX_MS = 12_000;
export const WORD_RATE = 40;
export const PHRASE_RATE = 150;
const PHRASE_MAX_CHARS = 80;

/** About 4 s for 100 characters, 11 s for 500, capped at PACE_MAX_MS. */
export function typingDuration(chars: number): number {
	if (chars <= 0) return 0;
	return Math.min(PACE_MAX_MS, Math.max(PACE_MIN_MS, 350 * Math.pow(chars, 0.55)));
}

export function paceUnit(charsPerSecond: number): PaceUnit {
	if (charsPerSecond < WORD_RATE) return 'char';
	return charsPerSecond <= PHRASE_RATE ? 'word' : 'phrase';
}

export function hashSeed(value: string): number {
	let hash = 2166136261;
	for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
	return hash >>> 0;
}

export function mulberry32(seed: number): () => number {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) >>> 0;
		let t = state;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

function words(text: string): string[] {
	return text.match(/\s+|\S+\s*/g) ?? [];
}

function phrases(text: string): string[] {
	const result: string[] = [];
	let current = '';
	for (const word of words(text)) {
		current += word;
		if (/[,;:.!?]$/.test(current.trimEnd()) || word.includes('\n') || current.length >= PHRASE_MAX_CHARS) {
			result.push(current);
			current = '';
		}
	}
	if (current) result.push(current);
	return result;
}

function punctuationFactor(piece: string): number {
	const last = piece.trimEnd().slice(-1);
	if (!last) return 1;
	if ('.!?'.includes(last)) return 2.5;
	if (',;:'.includes(last)) return 1.5;
	return 1;
}

/**
 * Per-keystroke delay multiplier: 14% of keystrokes are slow (1.5x to 2.2x), 12% come in quick
 * bursts (0.35x to 0.6x), and the rest land near 1x (0.8x to 1.2x).
 */
export function keystrokeFactor(random: () => number): number {
	const roll = random();
	if (roll < 0.14) return 1.5 + random() * 0.7;
	if (roll < 0.26) return 0.35 + random() * 0.25;
	return 0.8 + random() * 0.4;
}

/** Turn text and a seed into reveal steps whose delays sum to the target duration. */
export function typingSchedule(text: string, seed: number): PaceSchedule {
	const total = text.length;
	if (total === 0) return { steps: [], totalMs: 0, unit: 'char' };
	const targetMs = typingDuration(total);
	const unit = paceUnit(total / (targetMs / 1000));
	// Array.from walks code points, so a surrogate pair stays one step.
	const pieces = unit === 'char' ? Array.from(text) : unit === 'word' ? words(text) : phrases(text);
	const random = mulberry32(seed);
	const perChar = targetMs / total;
	const raw = pieces.map((piece, index) => {
		let delay = piece.length * perChar * keystrokeFactor(random) * punctuationFactor(piece);
		if (/\n\s*\n/.test(piece) || (piece === '\n' && pieces[index - 1] === '\n')) delay += 300 + random() * 300;
		else if (unit !== 'phrase' && piece.trimEnd() !== piece && random() < 0.06) delay += 200 + random() * 300;
		return delay;
	});
	const scale = targetMs / raw.reduce((a, b) => a + b, 0);
	const steps = pieces.map((piece, index) => ({ chars: piece.length, delayMs: raw[index] * scale }));
	return { steps, totalMs: targetMs, unit };
}

/** Reveal time of each step: the first shows at once, each next after the prior step wait. */
export function stepTimes(steps: PaceStep[]): number[] {
	const times: number[] = [];
	let at = 0;
	for (const step of steps) {
		times.push(at);
		at += step.delayMs;
	}
	return times;
}

/** Characters visible once elapsedMs of the schedule have passed. */
export function shownAt(steps: PaceStep[], times: number[], elapsedMs: number): number {
	let shown = 0;
	for (let i = 0; i < steps.length && times[i] <= elapsedMs; i++) shown += steps[i].chars;
	return shown;
}

/** The agent selects text as a person drags: 200 ms plus 6 ms per character, at most 700 ms. */
export function selectSweepMs(chars: number): number {
	return Math.min(700, 200 + 6 * Math.max(0, chars));
}

/** Thinking-and-writing pause before an agent comment card appears: 600 ms plus 15 ms per body character, at most 5 s. */
export function commentHoldMs(bodyChars: number): number {
	return Math.min(5_000, 600 + 15 * Math.max(0, bodyChars));
}

/** Total display time of a comment arrival: the selection sweep over the anchor, then the hold. */
export function commentArrivalDelay(anchorChars: number, bodyChars: number): number {
	return selectSweepMs(anchorChars) + commentHoldMs(bodyChars);
}

/** How long replaced or deleted text stays selected before the first keystroke removes it: 400-600 ms. */
export function ghostHoldMs(random: () => number): number {
	return 400 + random() * 200;
}
