import { tick } from 'svelte';
import { derived, get, writable } from 'svelte/store';
import { shellInfo } from '$lib/remoteShell';
import { failureCopy, failureFromOperation, failureFromVerdict, type FailureCopy } from './failureCopy';
import { isFailedOperation, isInFlight, isResolvedFailure, type UpdateFailureCode, type UpdateOperation } from './model';
import { CREEP_STALE_MS, drainCount, NO_PROCESS_WINDOW_MS, ProgressModel, remoteEstimate, STEP_LABEL, TAKING_LONGER_MS, type ProgressInput, type StepId } from './screenProgress';
import { markOutcomeSeen } from './outcome';
import { startupProgress, type StartupProgress } from './startupProgress';
import { updateConfirmed, updateFailureVerdict, updateStatus, updateUnreachable } from './store';

export type ScreenMode = 'progress' | 'longer' | 'start_failed' | 'failed' | 'done';

export interface UpdateScreenView {
	mode: ScreenMode;
	step: StepId;
	label: string;
	/** The percentage on screen and bar, with one decimal until confirmed. */
	percent: number;
	/** A remote client's time-based guess, shown with "about". */
	estimated: boolean;
	fromVersion: string | null;
	toVersion: string | null;
	failure: FailureCopy | null;
	serviceDown: boolean;
}

/** After the new version is confirmed the screen stays this long at 100%, then leaves. */
export const EXIT_HOLD_MS = 1_000;
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
	estimateFrom: number | null;
	failure: { code: UpdateFailureCode | null; detail: string | null; verdict: boolean } | null;
	doneAt: number | null;
}

let latch: Latch | null = null;
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

function httpStep(operation: UpdateOperation | null): StepId {
	switch (operation?.phase) {
		case 'restarting': return 'restarting';
		case 'resuming': return 'starting';
		case 'verifying': return 'verifying';
		default: return operation && !isInFlight(operation) ? 'verifying' : 'waiting';
	}
}

function readSources(now: number, l: Latch, operation: UpdateOperation | null, unreachable: boolean, file: StartupProgress | null): { input: ProgressInput; longer: boolean; estimated: boolean } {
	const usual = get(updateStatus)?.usual_seconds ?? 45;
	if (!unreachable) {
		l.downAt = null;
		l.lastSeq = null;
		l.seqChangedAt = null;
		l.estimateFrom = null;
		const step = httpStep(operation);
		const drain = step === 'waiting' ? drainCount(operation?.drain_total, operation?.blocking) : null;
		return { input: { step, count: drain, alive: true }, longer: false, estimated: false };
	}
	l.downAt ??= now;
	if (shellInfo()?.mode !== 'local') {
		l.estimateFrom ??= l.model.shown;
		const elapsed = now - l.downAt;
		l.model.lift(remoteEstimate(l.estimateFrom, elapsed, usual));
		return { input: { step: 'restarting', count: null, alive: false }, longer: elapsed >= usual * 2_000, estimated: true };
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

function currentFailure(l: Latch, operation: UpdateOperation | null): Latch['failure'] {
	if (operation?.operation_id === dismissedFailureOperationId) return null;
	const verdict = get(updateFailureVerdict);
	if (verdict?.kind === 'version_mismatch') return { code: null, detail: verdict.message, verdict: true };
	if (operation && isFailedOperation(operation) && !isResolvedFailure(operation)) {
		return { code: operation.failure_code ?? null, detail: operation.failure_detail ?? null, verdict: false };
	}
	return l.failure;
}

function copyFor(failure: NonNullable<Latch['failure']>, operation: UpdateOperation | null): FailureCopy {
	if (failure.verdict) return failureCopy(failureFromVerdict({ kind: 'version_mismatch', message: failure.detail ?? '' }));
	if (operation && isFailedOperation(operation)) return failureCopy(failureFromOperation(operation));
	return failureCopy({ code: failure.code, detail: failure.detail, summary: null, conductorTold: false, log: null });
}

/** Recompute from the existing update operation, current PID-validated file, and confirmed-version cue. */
export function tickUpdateScreen(now = Date.now()): void {
	const status = get(updateStatus);
	const operation = status?.operation ?? null;
	if (!latch) {
		if (operation?.operation_id === completedOperationId) return;
		if (operation && completedOperationId !== null) completedOperationId = null;
		const terminalFailure = operation?.kind === 'update' && isFailedOperation(operation)
			&& !isResolvedFailure(operation) && !operation.outcome_seen_at && operation.failure_code !== 'restart_pending'
			&& operation.operation_id !== dismissedFailureOperationId;
		if (!activeOperation(operation) && !terminalFailure) return;
		if (operation.operation_id !== dismissedFailureOperationId) dismissedFailureOperationId = null;
		latch = {
			operationId: operation.operation_id, model: new ProgressModel(),
			fromVersion: status?.current_version ?? null, toVersion: operation.target_version ?? status?.target_version ?? null,
			downAt: null, seqChangedAt: null, lastSeq: null, lastPid: null, estimateFrom: null, failure: null, doneAt: null,
		};
	}
	const l = latch;
	const own = operation && operation.operation_id === l.operationId ? operation : null;
	if (operation && operation.operation_id !== l.operationId && activeOperation(operation)) {
		latch = null;
		tickUpdateScreen(now);
		return;
	}
	const unreachable = get(updateUnreachable);
	const file = get(startupProgress)?.file ?? null;
	const confirmation = get(updateConfirmed);
	const currentConfirmation = confirmation?.operationId === l.operationId ? confirmation : null;
	l.toVersion = own?.target_version ?? l.toVersion;
	// The progress file's schema and version fields are intentionally never used as app-version labels.

	l.failure = currentFailure(l, own);
	if (l.failure && !currentConfirmation) {
		const copy = copyFor(l.failure, own);
		updateScreenView.set({ mode: 'failed', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: copy, serviceDown: unreachable });
		return;
	}
	if (currentConfirmation) {
		if (l.doneAt === null) {
			l.doneAt = now;
			completedOperationId = l.operationId;
			l.toVersion = currentConfirmation.version ?? l.toVersion;
			exitTimer = setTimeout(() => resetUpdateScreen(true), EXIT_HOLD_MS);
		}
		updateScreenView.set({ mode: 'done', step: 'verifying', label: STEP_LABEL.verifying, percent: 100, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: false });
		return;
	}
	if (unreachable && file?.phase === 'failed') {
		updateScreenView.set({ mode: 'start_failed', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated: false, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: true });
		return;
	}
	const { input, longer, estimated } = readSources(now, l, own, unreachable, file);
	l.model.update(now, input);
	updateScreenView.set({ mode: longer ? 'longer' : 'progress', step: l.model.step, label: STEP_LABEL[l.model.step], percent: l.model.percent, estimated, fromVersion: l.fromVersion, toVersion: l.toVersion, failure: null, serviceDown: unreachable });
}

/** Dismiss a terminal service-up update failure without letting stale status polls remount it. */
export function dismissUpdateScreen(): void {
	const view = get(updateScreenView);
	if (!latch || view?.mode !== 'failed' || view.serviceDown) return;
	dismissedFailureOperationId = latch.operationId;
	updateFailureVerdict.set(null);
	void markOutcomeSeen(latch.operationId);
	resetUpdateScreen(true);
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

/** Follow update state for as long as the app shell is mounted. */
export function startUpdateScreen(onReturnToChat?: () => void | Promise<void>): () => void {
	stopUpdateScreen();
	returnToChat = onReturnToChat;
	const recompute = () => tickUpdateScreen();
	unsubscribers = [updateStatus, updateUnreachable, startupProgress, updateConfirmed, updateFailureVerdict].map((source) => source.subscribe(recompute));
	timer = setInterval(recompute, 1_000);
	return stopUpdateScreen;
}

export function stopUpdateScreen(): void {
	if (timer !== null) clearInterval(timer);
	timer = null;
	for (const unsubscribe of unsubscribers) unsubscribe();
	unsubscribers = [];
	returnToChat = undefined;
	dismissedFailureOperationId = null;
	resetUpdateScreen(false);
}
