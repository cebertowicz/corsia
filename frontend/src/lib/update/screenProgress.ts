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

/** An update whose startup migrates the data has all seven steps; one that does not skips three. */
export type BandMode = 'migration' | 'plain';

type Band = { start: number; end: number; /** Seconds to cover half the remaining distance. */ halfLife: number };

/**
 * Band edges tuned on the constructed 24 GB replay in the approved U6 evidence.
 * Counts are lower bounds inside their own step. They are presentation bounds, not completion fractions.
 */
export const BANDS: Record<BandMode, Partial<Record<StepId, Band>>> = {
	migration: {
		waiting: { start: 0, end: 12, halfLife: 20 },
		restarting: { start: 12, end: 16, halfLife: 6 },
		backing_up: { start: 16, end: 38, halfLife: 40 },
		upgrading: { start: 38, end: 68, halfLife: 59 },
		checking_data: { start: 68, end: 76, halfLife: 15 },
		starting: { start: 76, end: 96, halfLife: 30 },
		verifying: { start: 96, end: 100, halfLife: 4 },
	},
	plain: {
		waiting: { start: 0, end: 15, halfLife: 20 },
		restarting: { start: 15, end: 35, halfLife: 6 },
		starting: { start: 35, end: 90, halfLife: 40 },
		verifying: { start: 90, end: 100, halfLife: 4 },
	},
};

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
	/** Whether the source proves the service is making progress; false stops the creep. */
	alive: boolean;
}

export class ProgressModel {
	/** The exact value, always inside the current band. */
	value = 0;
	step: StepId = 'waiting';
	mode: BandMode = 'migration';
	/** A remote client's estimate: it lifts the shown number without moving the band the model follows. */
	private lifted = 0;
	private visitedMigration = false;
	private lastAt: number | null = null;

	private band(step: StepId): Band {
		return BANDS[this.mode][step] ?? BANDS.migration[step]!;
	}

	update(now: number, input: ProgressInput): number {
		let { step, count } = input;
		if (stepOrder(step) < stepOrder(this.step)) { step = this.step; count = null; }
		if (step === 'backing_up' || step === 'upgrading' || step === 'checking_data') this.visitedMigration = true;
		if (step === 'starting' && !this.visitedMigration) this.mode = 'plain';
		if (step !== this.step) this.step = step;
		const band = this.band(step);
		const dt = this.lastAt === null ? 0 : Math.max(0, (now - this.lastAt) / 1000);
		this.lastAt = now;
		this.value = Math.max(this.value, band.start);
		if (input.alive && dt > 0 && this.value < band.end) {
			this.value = band.end - (band.end - this.value) * 0.5 ** (dt / band.halfLife);
		}
		if (count && count.total > 0) {
			const counted = band.start + (band.end - band.start) * Math.min(1, Math.max(0, count.done / count.total));
			this.value = Math.max(this.value, Math.min(counted, band.end));
		}
		this.value = Math.min(this.value, band.end);
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

	/** One decimal on screen and bar; 100 only once the new version is confirmed. */
	get percent(): number {
		return Math.min(99.9, Math.floor(this.shown * 10 + 1e-8) / 10);
	}

	/** Everything the model carries, so the same screen can continue on another page without resetting. */
	snapshot(): ProgressSnapshot {
		return { value: this.value, step: this.step, mode: this.mode, lifted: this.lifted, visitedMigration: this.visitedMigration, lastAt: this.lastAt };
	}

	static restore(snapshot: ProgressSnapshot): ProgressModel {
		const model = new ProgressModel();
		model.value = snapshot.value;
		model.step = snapshot.step;
		model.mode = snapshot.mode;
		model.lifted = snapshot.lifted;
		model.visitedMigration = snapshot.visitedMigration;
		model.lastAt = snapshot.lastAt;
		return model;
	}
}

export interface ProgressSnapshot {
	value: number;
	step: StepId;
	mode: BandMode;
	lifted: number;
	visitedMigration: boolean;
	lastAt: number | null;
}

/** Drain progress as a count: agents already safe out of those the drain waited on. */
export function drainCount(total: number | null | undefined, blocking: number | null | undefined): { done: number; total: number } | null {
	if (!total || total <= 0) return null;
	return { done: total - Math.min(total, blocking ?? total), total };
}

/**
 * A remote client cannot see the restart, so its time-based guess stops where the restart step ends: it
 * never claims a step it has not seen. Climbing on to 95 on the clock alone left the number far from the
 * first real step it met when the service answered, and the screen jumped across the gap.
 */
export const REMOTE_ESTIMATE_CEILING = BANDS.plain.restarting!.end;

/** A remote client estimates only from its last confirmed screen value and the usual restart time. */
export function remoteEstimate(lastSeen: number, elapsedMs: number, usualSeconds: number): number {
	if (lastSeen >= REMOTE_ESTIMATE_CEILING) return lastSeen;
	const usual = Math.max(1, usualSeconds) * 1000;
	const share = Math.min(1, Math.max(0, elapsedMs / usual));
	return lastSeen + (REMOTE_ESTIMATE_CEILING - lastSeen) * share;
}
