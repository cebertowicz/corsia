import { tick } from 'svelte';
import { derived, get, writable } from 'svelte/store';
import { shellInfo } from '$lib/remoteShell';
import { toasts } from '$lib/stores/toast';
import type { FailureCopy } from './failureCopy';
import { agentsSafe, isFailedOperation, isInFlight, isResolvedFailure, type UpdateOperation } from './model';
import { copyFor, currentFailure, type ScreenFailure } from './screenFailure';
import { CREEP_STALE_MS, drainCount, NO_PROCESS_WINDOW_MS, PHASE_SECONDS, ProgressModel, remoteEstimate, STEP_LABEL, TAKING_LONGER_MS, type ProgressInput, type StepId } from './screenProgress';
import { markOutcomeSeen } from './outcome';
import type { ScreenHandoff } from './shellHandoff';
import { startupProgress, type StartupProgress } from './startupProgress';
import { updateConfirmed, updateFailureVerdict, updateStatus, updateUnreachable, type UpdateStatus } from './store';

export type ScreenMode = 'progress' | 'longer' | 'start_failed' | 'failed' | 'done';

export interface UpdateScreenView {
	operationId: string;
	mode: ScreenMode;
	step: StepId;
	label: string;
	/** The real percentage; the screen counts a whole number up to it and the bar follows that number. */
	percent: number;
	/** A remote client's time-based guess while the main computer's service is down. */
	estimated: boolean;
	fromVersion: string | null;
	toVersion: string | null;
	failure: FailureCopy | null;
	serviceDown: boolean;
	/** The desktop shell cannot currently read the PID-validated local progress record. */
	startupProgressUnavailable?: boolean;
	paused?: { safe: number; total: number } | null;
}

/** After the new version is confirmed the screen stays this long at 100%, then leaves. */
export const EXIT_HOLD_MS = 1_000;
/** A paired computer with no usual time allows twice the longest expected outage (a restart with a data upgrade), as it allows twice a known usual time. */
export const NO_HISTORY_LONGER_MS = 2_000 * (PHASE_SECONDS.restarting + PHASE_SECONDS.backing_up + PHASE_SECONDS.upgrading
	+ PHASE_SECONDS.checking_data + PHASE_SECONDS.starting);
export const updateScreenView = writable<UpdateScreenView | null>(null);
export const updateScreenVisible = derived(updateScreenView, (view) => view !== null);

const FILE_STEP: Record<Exclude<StartupProgress['phase'], 'failed'>, StepId> = {
	starting: 'restarting', backing_up: 'backing_up', migrating: 'upgrading', validating: 'checking_data', recovering: 'starting', serving: 'starting',
};

interface Latch {
	operationId: string;
	model: ProgressModel;
	fromVersion: string | null;
	toVersion: string | null;
	downAt: number | null;
	seqChangedAt: number | null;
	lastSeq: number | null;
	lastPid: number | null;
	lateReading: { pid: number; seq: number } | null;
	estimateFrom: number | null;
	failure: ScreenFailure | null;
	doneAt: number | null;
	usualSeconds: number | null;
	drainDeadline: number | null;
	lastStatus: UpdateStatus | null;
	paused: { safe: number; total: number } | null;
}

let latch: Latch | null = null;
/** A screen another page handed over, waiting for this page's own reading of the same operation. */
let handoff: { screen: ScreenHandoff; serviceDown: boolean } | null = null;
let dismissedFailureOperationId: string | null = null;
/** A confirmed operation stays gone while the last status poll still calls it in flight. */
let completedOperationId: string | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let exitTimer: ReturnType<typeof setTimeout> | null = null;
let returnToChat: (() => void | Promise<void>) | undefined;
let unsubscribers: Array<() => void> = [];

function activeOperation(operation: UpdateOperation | null | undefined): operation is UpdateOperation {
	if (!operation || !isInFlight(operation) || operation.kind !== 'update') return false;
	// The existing holding phase stays in the toast. The screen begins only when the operation enters restart work.
	return operation.phase === 'steering' || operation.phase === 'draining' || operation.phase === 'ready'
		|| operation.phase === 'restarting' || operation.phase === 'resuming' || operation.phase === 'verifying';
}

/** Only these durable results prove the restart began and failed; preparation failures stay in the toast and Settings. */
function failedAfterRestart(operation: UpdateOperation | null | undefined): boolean {
	return operation?.failure_code === 'rolled_back' || operation?.failure_code === 'version_unverified'
		|| operation?.failure_code === 'wrong_version_running';
}

function httpStep(operation: UpdateOperation | null): StepId {
	switch (operation?.phase) {
		case 'ready': case 'restarting': return 'restarting';
		case 'resuming': return 'starting';
		case 'verifying': return 'verifying';
		default: return operation && !isInFlight(operation) ? 'verifying' : 'waiting';
	}
}

function readSources(now: number, l: Latch, operation: UpdateOperation | null, unreachable: boolean, file: StartupProgress | null, readError: 'unavailable' | null, hold: boolean): { input: ProgressInput; longer: boolean; estimated: boolean } {
	const usual = get(updateStatus)?.usual_seconds;
	if (typeof usual === 'number' && Number.isFinite(usual) && usual > 0) l.usualSeconds = usual;
	if (!unreachable) {
		l.downAt = null;
		l.lastSeq = null;
		l.seqChangedAt = null;
		l.lateReading = null;
		l.estimateFrom = null;
		const step = httpStep(operation);
		const drain = step === 'waiting' ? drainCount(operation?.drain_total, operation?.blocking) : null;
		return { input: { step, count: drain, alive: true, drainRemaining: step === 'waiting' ? operation?.hard_cap_remaining ?? null : null }, longer: false, estimated: false };
	}
	l.downAt ??= now;
	if (readError) return { input: { step: 'restarting', count: null, alive: false }, longer: false, estimated: false };
	if (shellInfo()?.mode !== 'local') {
		if (l.usualSeconds === null) return { input: { step: 'restarting', count: null, alive: false }, longer: now - l.downAt >= NO_HISTORY_LONGER_MS, estimated: false };
		l.estimateFrom ??= l.model.shown;
		const elapsed = now - l.downAt;
		if (!hold) l.model.lift(remoteEstimate(l.estimateFrom, now - Math.max(l.downAt, l.drainDeadline ?? l.downAt)));
		return { input: { step: 'restarting', count: null, alive: false }, longer: elapsed >= l.usualSeconds * 2_000, estimated: true };
	}
	// A still-running old service can report serving as HTTP goes away. Its PID is valid, but it says nothing about
	// the replacement. Only accept its late startup phases after an earlier phase from that same PID was observed.
	const latePhase = file?.phase === 'recovering' || file?.phase === 'serving';
	if (file && latePhase && file.pid !== l.lastPid) {
		if (l.lastPid === null && file.seq !== l.lastSeq) {
			l.lastSeq = file.seq;
			l.seqChangedAt = now;
		} else if (l.lastPid !== null) {
			if (l.lateReading?.pid === file.pid && l.lateReading.seq !== file.seq) l.seqChangedAt = now;
			l.lateReading = { pid: file.pid, seq: file.seq };
		}
		const frozen = now - (l.seqChangedAt ?? now);
		return { input: { step: 'restarting', count: null, alive: frozen < CREEP_STALE_MS }, longer: frozen >= TAKING_LONGER_MS, estimated: false };
	}
	if (file && file.phase !== 'failed') {
		if (file.seq !== l.lastSeq || file.pid !== l.lastPid) {
			l.lastSeq = file.seq;
			l.lastPid = file.pid;
			l.seqChangedAt = now;
		}
		const frozen = now - (l.seqChangedAt ?? now);
		const count = (file.phase === 'backing_up' || file.phase === 'migrating') && file.total ? { done: file.done ?? 0, total: file.total } : null;
		return { input: { step: FILE_STEP[file.phase], count, alive: frozen < CREEP_STALE_MS }, longer: frozen >= TAKING_LONGER_MS, estimated: false };
	}
	const since = Math.max(l.downAt, l.seqChangedAt ?? 0);
	return { input: { step: 'restarting', count: null, alive: now - since < NO_PROCESS_WINDOW_MS }, longer: now - since >= NO_PROCESS_WINDOW_MS, estimated: false };
}

/** Recompute from the existing update operation, current PID-validated file, and confirmed-version cue. */
export function tickUpdateScreen(now = Date.now()): void {
	const status = get(updateStatus);
	const operation = status?.operation ?? null;
	if (!latch && handoff) adoptHandoff(operation);
	if (!latch) {
		if (operation?.operation_id === completedOperationId) return;
		if (operation && completedOperationId !== null) completedOperationId = null;
		const terminalFailure = operation?.kind === 'update' && isFailedOperation(operation)
			&& failedAfterRestart(operation) && !isResolvedFailure(operation) && !operation.outcome_seen_at
			&& operation.operation_id !== dismissedFailureOperationId;
		if (!activeOperation(operation) && !terminalFailure) return;
		if (operation.operation_id !== dismissedFailureOperationId) dismissedFailureOperationId = null;
		latch = {
			operationId: operation.operation_id, model: new ProgressModel(),
			fromVersion: status?.current_version ?? null, toVersion: operation.target_version ?? status?.target_version ?? null,
			downAt: null, seqChangedAt: null, lastSeq: null, lastPid: null, lateReading: null, estimateFrom: null, failure: null, doneAt: null,
				usualSeconds: typeof status?.usual_seconds === 'number' && Number.isFinite(status.usual_seconds) && status.usual_seconds > 0 ? status.usual_seconds : null,
			drainDeadline: null, lastStatus: null, paused: null,
		};
	}
	const l = latch;
	const own = operation && operation.operation_id === l.operationId ? operation : null;
	if (status !== l.lastStatus) {
		l.lastStatus = status;
		if (own && httpStep(own) === 'waiting') l.paused = agentsSafe(own);
		if (own?.phase === 'draining' && typeof own.hard_cap_remaining === 'number' && Number.isFinite(own.hard_cap_remaining)) {
			l.drainDeadline = now + Math.max(0, own.hard_cap_remaining) * 1_000;
		} else if (own && ['ready', 'restarting', 'resuming', 'verifying'].includes(own.phase ?? '')) l.drainDeadline = null;
	}
	if (operation && operation.operation_id !== l.operationId && activeOperation(operation)) {
		latch = null;
		if (exitTimer !== null) clearTimeout(exitTimer);
		exitTimer = null;
		tickUpdateScreen(now);
		return;
	}
	const unreachable = get(updateUnreachable);
	const progress = get(startupProgress);
	const file = progress?.file ?? null;
	const confirmation = get(updateConfirmed);
	const currentConfirmation = confirmation?.operationId === l.operationId ? confirmation : null;
	l.toVersion = own?.target_version ?? l.toVersion;
	// The progress file's schema and version fields are intentionally never used as app-version labels.

	l.failure = currentFailure(l.failure, own, dismissedFailureOperationId);
	if (l.failure && !currentConfirmation) {
		updateScreenView.set({ operationId: l.operationId, mode: 'failed', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: copyFor(l.failure, own, unreachable), serviceDown: unreachable, startupProgressUnavailable: false });
		return;
	}
	if (currentConfirmation) {
		if (l.doneAt === null) {
			l.doneAt = now;
			completedOperationId = l.operationId;
			l.toVersion = currentConfirmation.version ?? l.toVersion;
			exitTimer = setTimeout(() => resetUpdateScreen(true), EXIT_HOLD_MS);
		}
		updateScreenView.set({ operationId: l.operationId, mode: 'done', step: 'verifying', label: STEP_LABEL.verifying, percent: 100, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: false, startupProgressUnavailable: false });
		return;
	}
	if (unreachable && file?.phase === 'failed') {
		updateScreenView.set({ operationId: l.operationId, mode: 'start_failed', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: true, startupProgressUnavailable: false });
		return;
	}
	const restartSeen = own && ['ready', 'restarting', 'resuming', 'verifying'].includes(own.phase ?? '');
	// Early startup phases are evidence of the replacement process; a late serving record can belong to the old one.
	const startupSeen = shellInfo()?.mode === 'local' && file && ['starting', 'backing_up', 'migrating', 'validating'].includes(file.phase);
	const hold = unreachable && l.model.step === 'waiting' && !restartSeen && !startupSeen && (l.drainDeadline === null || now < l.drainDeadline);
	const { input, longer, estimated } = readSources(now, l, own, unreachable, file, progress?.readError ?? null, hold);
	l.model.update(now, hold ? { step: 'waiting', count: drainCount(own?.drain_total, own?.blocking), alive: false, drainRemaining: own?.hard_cap_remaining ?? null } : input);
	updateScreenView.set({ operationId: l.operationId, mode: longer ? 'longer' : 'progress', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated: hold ? false : estimated, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: unreachable, startupProgressUnavailable: unreachable && progress?.readError === 'unavailable', paused: hold ? l.paused : null });
}

/** Dismiss a terminal service-up update failure without letting stale status polls remount it. */
export function dismissUpdateScreen(): void {
	const view = get(updateScreenView);
	if (!latch || view?.mode !== 'failed' || view.serviceDown) return;
	const operation = get(updateStatus)?.operation;
	dismissedFailureOperationId = latch.operationId;
	updateFailureVerdict.set(null);
	// The store owns the keyed terminal notice. It is this outcome only while the current durable row still names it.
	if (operation?.operation_id === latch.operationId && isFailedOperation(operation)) toasts.unpin('update');
	void markOutcomeSeen(latch.operationId);
	resetUpdateScreen(true);
}

function adoptHandoff(operation: UpdateOperation | null): void {
	const { screen, serviceDown } = handoff!;
	// The shell's page reads no update status: the service is down, which is why it holds the screen. A local-service
	// page continues the screen only once its own status names the same operation.
	if (!serviceDown && !operation) return;
	handoff = null;
	// An outcome some device already showed is history; the screen it was waiting for is gone.
	if (!serviceDown && (operation!.operation_id !== screen.operationId || (!isInFlight(operation) && operation!.outcome_seen_at))) return;
	latch = {
		operationId: screen.operationId, model: ProgressModel.restore(screen.model),
		fromVersion: screen.fromVersion, toVersion: screen.toVersion,
		downAt: screen.downAt, seqChangedAt: screen.seqChangedAt, lastSeq: screen.lastSeq, lastPid: screen.lastPid,
		lateReading: null, estimateFrom: null, failure: screen.failure && { code: screen.failure.code, detail: null, verdict: screen.failure.verdict },
		doneAt: null, usualSeconds: screen.usualSeconds, drainDeadline: screen.drainDeadline ?? null, lastStatus: null, paused: screen.paused ?? null,
	};
}

/** Continue a screen another page handed over, from the next recompute on. */
export function continueHandedOverScreen(screen: ScreenHandoff, serviceDown: boolean): void {
	handoff = { screen, serviceDown };
	tickUpdateScreen();
}

/** The screen as it stands, for the page it moves to; null when no update screen is showing. */
export function handOverScreen(theme: ScreenHandoff['theme'], route: string): ScreenHandoff | null {
	if (!latch) return null;
	const failure = latch.failure && { code: latch.failure.code, verdict: latch.failure.verdict };
	return {
		v: 1, operationId: latch.operationId, model: latch.model.snapshot(), fromVersion: latch.fromVersion, toVersion: latch.toVersion,
		usualSeconds: latch.usualSeconds, drainDeadline: latch.drainDeadline, paused: latch.paused, downAt: latch.downAt, seqChangedAt: latch.seqChangedAt, lastSeq: latch.lastSeq, lastPid: latch.lastPid,
		failure, theme, route,
	};
}

/** Leave the screen. Returning to chat hands the composer its focus back. */
export function resetUpdateScreen(focusComposer = false): void {
	latch = null;
	if (exitTimer !== null) clearTimeout(exitTimer);
	exitTimer = null;
	updateScreenView.set(null);
	updateConfirmed.set(null);
	if (!focusComposer || typeof document === 'undefined') return;
	void Promise.resolve(returnToChat?.()).then(tick).then(() => {
		document.querySelector<HTMLElement>('[role="textbox"][aria-label="Message CORSIA"]')?.focus();
	});
}

/** Follow update state while mounted; the static shell shares this clock for its native progress reading. */
export function startUpdateScreen(onReturnToChat?: () => void | Promise<void>, onClockTick?: () => void): () => void {
	stopUpdateScreen();
	returnToChat = onReturnToChat;
	const recompute = () => tickUpdateScreen();
	unsubscribers = [updateStatus, updateUnreachable, startupProgress, updateConfirmed, updateFailureVerdict].map((source) => source.subscribe(recompute));
	timer = setInterval(() => { onClockTick?.(); recompute(); }, 1_000);
	return stopUpdateScreen;
}

export function stopUpdateScreen(): void {
	if (timer !== null) clearInterval(timer);
	timer = null;
	for (const unsubscribe of unsubscribers) unsubscribe();
	unsubscribers = [];
	returnToChat = undefined;
	dismissedFailureOperationId = null;
	handoff = null;
	resetUpdateScreen(false);
}
