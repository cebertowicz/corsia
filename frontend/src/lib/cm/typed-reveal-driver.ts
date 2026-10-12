import { StateEffect } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { AGENT_PRESENT_FOR } from '$lib/docPresence';
import { hashSeed, shownAt, stepTimes, typingSchedule, type PaceStep } from '$lib/typingPace';
import { clampPresenceLabelsNow, LABEL_FADE_MS } from './presence-marks';
import { backlogCount, backlogText } from './typed-reveal-preview';
import { endSelect, selectShown } from './typed-reveal-select';
import { dropLane, laneState, revealAll, revealCount, typedReveals, type Lane } from './typed-reveal-state';

/** The caret holds steady for this long after the last character, then blinks. */
export const TYPING_IDLE_MS = 200;
export { LABEL_FADE_MS };

interface Clock {
	generation: number;
	steps: PaceStep[];
	times: number[];
	startedAt: number;
	applied: number;
	lastAdvanceAt: number;
	/** The select step this clock times (sweep, then hold), and when it began. */
	selectKey: string | null;
	selectStartedAt: number;
	/** When a select step paused this lane's typing; the schedule resumes where it was once the select ends. */
	pausedAt: number | null;
}

/**
 * Paces each lane on animation frames while text types or a selection sweeps; otherwise one timer
 * waits for the next due change (a hold ending, the label fading, the lane dropping). A lane pace
 * is recomputed whenever its backlog grows.
 */
export const typingDriver = ViewPlugin.fromClass(class {
	private readonly clocks = new Map<string, Clock>();
	private frame: number | null = null;
	private timer: ReturnType<typeof setTimeout> | null = null;
	private readonly onVisibility = () => {
		if (document.visibilityState === 'hidden') this.view.dispatch({ effects: revealAll.of(null) });
	};

	constructor(private readonly view: EditorView) {
		document.addEventListener('visibilitychange', this.onVisibility);
	}

	update(update: ViewUpdate): void {
		const lanes = update.state.field(typedReveals);
		const live = new Set(lanes.map((lane) => lane.threadId));
		for (const id of this.clocks.keys()) if (!live.has(id)) this.clocks.delete(id);
		const now = performance.now();
		for (const lane of lanes) this.sync(lane, update, now);
		this.schedule(now);
	}

	destroy(): void {
		document.removeEventListener('visibilitychange', this.onVisibility);
		if (this.frame !== null) cancelAnimationFrame(this.frame);
		if (this.timer !== null) clearTimeout(this.timer);
		this.frame = this.timer = null;
		this.clocks.clear();
	}

	private sync(lane: Lane, update: ViewUpdate, now: number): void {
		const clock = this.clocks.get(lane.threadId);
		if (clock && clock.selectKey !== (lane.select?.key ?? null)) {
			clock.selectKey = lane.select?.key ?? null;
			clock.selectStartedAt = now;
		}
		if (clock && clock.generation === lane.generation) return;
		const { steps } = backlogCount(lane)
			? typingSchedule(backlogText(update.state, lane), hashSeed(`${lane.threadId}:${lane.generation}`))
			: { steps: [] };
		this.clocks.set(lane.threadId, {
			generation: lane.generation, steps, times: stepTimes(steps), startedAt: now, applied: 0,
			lastAdvanceAt: clock?.lastAdvanceAt ?? now,
			selectKey: lane.select?.key ?? null, selectStartedAt: clock && clock.selectKey === (lane.select?.key ?? null) ? clock.selectStartedAt : now,
			pausedAt: null,
		});
	}

	private schedule(now: number): void {
		if (this.timer !== null) clearTimeout(this.timer);
		this.timer = null;
		if (this.frame !== null || !this.clocks.size) return;
		const due = this.nextDue(now);
		if (due === 'frame') this.frame = requestAnimationFrame(() => { this.frame = null; this.tick(); });
		else if (due !== null) this.timer = setTimeout(() => { this.timer = null; this.tick(); }, Math.max(0, Math.ceil(due - now)));
	}

	/** A frame while anything animates, else the earliest time a lane's state next changes, or null when none will. */
	private nextDue(now: number): 'frame' | number | null {
		let due: number | null = null;
		const at = (time: number) => { if (Number.isFinite(time)) due = due === null ? time : Math.min(due, time); };
		for (const lane of this.view.state.field(typedReveals)) {
			const clock = this.clocks.get(lane.threadId);
			if (!clock) continue;
			const select = lane.select;
			if (select) {
				if (select.shown < select.length || now - clock.selectStartedAt < select.sweepMs) return 'frame';
				at(clock.selectStartedAt + select.sweepMs + select.holdMs);
				continue;
			}
			if (backlogCount(lane) > 0 || clock.pausedAt !== null) return 'frame';
			if (lane.typing) at(clock.lastAdvanceAt + TYPING_IDLE_MS);
			if (!lane.quiet) at(clock.lastAdvanceAt + LABEL_FADE_MS);
			if (!lane.previews.length) at(clock.lastAdvanceAt + AGENT_PRESENT_FOR);
		}
		return due;
	}

	private tick(): void {
		const now = performance.now();
		const effects: StateEffect<unknown>[] = [];
		const lanes = this.view.state.field(typedReveals);
		for (const lane of lanes) {
			const clock = this.clocks.get(lane.threadId);
			if (!clock) continue;
			const hidden = backlogCount(lane);
			const selecting = lane.select !== null;
			if (!selecting && clock.pausedAt !== null) { clock.startedAt += now - clock.pausedAt; clock.pausedAt = null; }
			if (lane.select) this.selectStep(lane, clock, now, effects);
			if (hidden && !selecting) {
				const target = shownAt(clock.steps, clock.times, now - clock.startedAt);
				const finished = now - clock.startedAt >= (clock.times[clock.times.length - 1] ?? 0);
				const count = finished ? hidden : target - clock.applied;
				if (count > 0) {
					effects.push(revealCount.of({ threadId: lane.threadId, count }));
					clock.applied += count;
					clock.lastAdvanceAt = now;
				}
			}
			if (selecting) { clock.pausedAt ??= now; clock.lastAdvanceAt = now; }
			const sinceAdvance = now - clock.lastAdvanceAt;
			// A lane holding a preview waits for its commit or end, however long the model takes.
			if (!hidden && !selecting && !lane.previews.length && sinceAdvance >= AGENT_PRESENT_FOR) { effects.push(dropLane.of(lane.threadId)); continue; }
			const typing = (hidden > 0 && sinceAdvance < TYPING_IDLE_MS) || selecting;
			const quiet = hidden === 0 && !selecting && sinceAdvance >= LABEL_FADE_MS;
			if (typing !== lane.typing || quiet !== lane.quiet) effects.push(laneState.of({ threadId: lane.threadId, typing, quiet }));
		}
		if (effects.length) {
			this.view.dispatch({ effects });
			clampPresenceLabelsNow(this.view);
		}
		this.schedule(now);
	}

	/** Sweep the selection over `sweepMs`, hold it, then end it: this clock is the only one a select has. */
	private selectStep(lane: Lane, clock: Clock, now: number, effects: StateEffect<unknown>[]): void {
		const select = lane.select!;
		const elapsed = now - clock.selectStartedAt;
		const shown = select.sweepMs > 0 ? Math.min(select.length, Math.ceil(select.length * elapsed / select.sweepMs)) : select.length;
		if (shown !== select.shown) effects.push(selectShown.of({ threadId: lane.threadId, key: select.key, shown }));
		if (elapsed >= select.sweepMs + select.holdMs) effects.push(endSelect.of({ threadId: lane.threadId, key: select.key, outcome: 'ended' }));
	}
});
