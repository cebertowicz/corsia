/**
 * The update screen's percentage: pure bands, creep and stall rules, no timers and no stores.
 *
 * The number comes from real signals (the drain count, the service's progress file, the version
 * check). Inside a band it creeps only while the source proves it is alive, and it never goes down.
 */

export type StepId = 'waiting' | 'restarting' | 'backing_up' | 'upgrading' | 'checking_data' | 'starting' | 'verifying';

/** The step on screen, in plain words. */
export const STEP_LABEL: Record<StepId, string> = {
	waiting: 'Waiting for agents to finish',
	restarting: 'Restarting CORSIA',
	backing_up: 'Backing up your data',
	upgrading: 'Upgrading your data',
	checking_data: 'Checking your data',
	starting: 'Starting CORSIA',
	verifying: 'Checking the new version',
};

const ORDER: StepId[] = ['waiting', 'restarting', 'backing_up', 'upgrading', 'checking_data', 'starting', 'verifying'];

export function stepOrder(step: StepId): number {
	return ORDER.indexOf(step);
}

/**
 * Expected seconds per phase. The restart, startup and verify figures are from one measured update with no data
 * upgrade (drain to its cap, ~14 s from service stop to the new service starting, ~17 s of startup, a few seconds
 * to confirm the version). The three data steps come from the constructed 24 GB-shaped replay behind the approved
 * U6 evidence (backup ~25 s, migration ~60 s, check ~15 s); they are presentation estimates, not measurements.
 * The drain has no fixed figure: it is its own elapsed time plus the cap that is left.
 */
export const PHASE_SECONDS: Record<Exclude<StepId, 'waiting'>, number> = {
	restarting: 14, backing_up: 25, upgrading: 60, checking_data: 15, starting: 17, verifying: 2,
};
/** The drain's length when its status does not say how much of the cap is left. */
export const DEFAULT_DRAIN_SECONDS = 60;
/** The most the bar claims before a phase has actually ended. */
export const PHASE_HOLD = 0.9;
/** A phase past its expected time creeps toward the ceiling with a half-life of this many of its own lengths. */
const OVERRUN_HALF_LIFE = 4;
/** Everything but the confirmed new version stays below this. */
const CEILING = 99.9;

const isDataStep = (step: StepId): boolean => step === 'backing_up' || step === 'upgrading' || step === 'checking_data';

/** Creep stops when the service's `seq` has not advanced for this long. */
export const CREEP_STALE_MS = 15_000;
/** "This is taking longer than usual" once `seq` has not advanced for this long. */
export const TAKING_LONGER_MS = 60_000;
/** ...or once no process of the new service has shown for this long. */
export const NO_PROCESS_WINDOW_MS = 30_000;

export interface ProgressInput {
	step: StepId;
	/** The step's own count (drain agents, backup pages, migration steps), when its source reports one. */
	count: { done: number; total: number } | null;
	/** Whether the source proves the service is making progress; false stops the clock. */
	alive: boolean;
	/** Seconds left before the drain's hard cap, while draining. */
	drainRemaining?: number | null;
}

/** The share of the remaining distance the current phase covers, and how far into it the clock has run. */
interface Phase { from: number; to: number; expected: number; elapsed: number }

/**
 * Each phase has an expected length in seconds. When a phase starts, the distance from the current value to 99.9 is split
 * across this and the phases still to come in proportion to those lengths, so the bar never jumps to a band start and
 * never goes down. Inside a phase it moves linearly with that phase's own time, holding short of the share's end until
 * the phase really ends. A data upgrade is unknown up front: the update starts without those phases and re-splits when
 * one appears.
 */
export class ProgressModel {
	/** The exact value. */
	value = 0;
	step: StepId = 'waiting';
	/** A remote client's estimate: it lifts the shown number without moving the phase the model follows. */
	private lifted = 0;
	private visitedMigration = false;
	private lastAt: number | null = null;
	private phase: Phase | null = null;

	/** Expected seconds of each phase from `step` on, the data steps only once one has shown. */
	private remaining(step: StepId, drainSeconds: number): number[] {
		return ORDER.slice(stepOrder(step))
			.filter((id) => !isDataStep(id) || this.visitedMigration)
			.map((id) => (id === 'waiting' ? drainSeconds : PHASE_SECONDS[id]));
	}

	private begin(step: StepId, drainSeconds: number): void {
		const from = this.shown;
		const seconds = this.remaining(step, drainSeconds);
		const expected = seconds[0]!;
		const share = (Math.max(from, CEILING) - from) * (expected / seconds.reduce((sum, value) => sum + value, 0));
		this.phase = { from, to: from + share, expected, elapsed: 0 };
	}

	update(now: number, input: ProgressInput): number {
		let { step, count } = input;
		if (stepOrder(step) < stepOrder(this.step)) { step = this.step; count = null; }
		if (isDataStep(step)) this.visitedMigration = true;
		const drainSeconds = Math.max(1, input.drainRemaining ?? DEFAULT_DRAIN_SECONDS);
		if (step !== this.step || !this.phase) {
			this.step = step;
			this.begin(step, drainSeconds);
		}
		const phase = this.phase!;
		const dt = this.lastAt === null ? 0 : Math.max(0, (now - this.lastAt) / 1000);
		this.lastAt = now;
		if (input.alive) phase.elapsed += dt;
		const span = phase.to - phase.from;
		const at = (fraction: number) => phase.from + span * PHASE_HOLD * fraction;
		const countShare = count && count.total > 0 ? Math.min(1, Math.max(0, count.done / count.total)) : 0;
		let timeShare: number;
		if (step === 'waiting') {
			// The drain's length is live: its time share is the part of its own time already gone, and an agent count can beat it.
			const length = phase.elapsed + Math.max(0, input.drainRemaining ?? 0);
			timeShare = input.drainRemaining == null ? Math.min(1, phase.elapsed / phase.expected) : length > 0 ? phase.elapsed / length : 1;
			this.value = Math.max(this.value, Math.min(at(Math.max(timeShare, countShare)), CEILING));
		} else {
			// A phase's own count lifts the value; its time keeps adding on top, so a count that stands still does not stall the bar.
			timeShare = Math.min(1, phase.elapsed / phase.expected);
			const stepped = input.alive ? this.value + (span * PHASE_HOLD * dt) / phase.expected : this.value;
			this.value = Math.min(Math.max(Math.min(stepped, Math.max(this.value, at(1))), at(countShare)), CEILING);
		}
		// A phase that outlasts its expected time, or whose count has used up its share (one long migration statement), keeps creeping while its source is alive.
		if ((timeShare >= 1 || this.value >= at(1)) && input.alive && dt > 0) {
			this.value = CEILING - (CEILING - this.value) * 0.5 ** (dt / (OVERRUN_HALF_LIFE * phase.expected));
		}
		return this.value;
	}

	/** Raise the shown number to at least this value (never lowers it). */
	lift(value: number): void {
		this.lifted = Math.max(this.lifted, Math.min(value, 99));
	}

	/** The exact number on screen. */
	get shown(): number {
		return Math.max(this.value, this.lifted);
	}

	/** The real percentage to one decimal, never 100: that comes only once the new version is confirmed. The screen shows a whole number counting up to it. */
	get percent(): number {
		return Math.min(99.9, Math.floor(this.shown * 10 + 1e-8) / 10);
	}

	/** Everything the model carries, so the same screen can continue on another page without resetting. */
	snapshot(): ProgressSnapshot {
		return { value: this.value, step: this.step, lifted: this.lifted, visitedMigration: this.visitedMigration, lastAt: this.lastAt, phase: this.phase && { ...this.phase } };
	}

	static restore(snapshot: ProgressSnapshot): ProgressModel {
		const model = new ProgressModel();
		model.value = snapshot.value;
		model.step = snapshot.step;
		model.lifted = snapshot.lifted;
		model.visitedMigration = snapshot.visitedMigration;
		model.lastAt = snapshot.lastAt;
		model.phase = snapshot.phase && { ...snapshot.phase };
		return model;
	}
}

export interface ProgressSnapshot {
	value: number;
	step: StepId;
	lifted: number;
	visitedMigration: boolean;
	lastAt: number | null;
	/** Absent from a snapshot another version wrote; the phase then starts again from the current value. */
	phase: Phase | null;
}

/** Drain progress as a count: agents already safe out of those the drain waited on. */
export function drainCount(total: number | null | undefined, blocking: number | null | undefined): { done: number; total: number } | null {
	if (!total || total <= 0) return null;
	return { done: total - Math.min(total, blocking ?? total), total };
}

/** A remote client cannot see the restart, so its time-based guess stops where the restart's share of what is left ends and never claims a step it has not seen. */
export function remoteCeiling(lastSeen: number): number {
	const left = PHASE_SECONDS.restarting + PHASE_SECONDS.starting + PHASE_SECONDS.verifying;
	return lastSeen + (CEILING - lastSeen) * (PHASE_SECONDS.restarting / left) * PHASE_HOLD;
}

/** A remote client estimates only from its last confirmed screen value and the usual restart time. */
export function remoteEstimate(lastSeen: number, elapsedMs: number, usualSeconds: number): number {
	const usual = Math.max(1, usualSeconds) * 1000;
	const share = Math.min(1, Math.max(0, elapsedMs / usual));
	return lastSeen + (remoteCeiling(lastSeen) - lastSeen) * share;
}
