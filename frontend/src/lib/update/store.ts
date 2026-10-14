import { derived, get, writable } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { restartSystem } from '$lib/stores/settingsSystemRestart';
import {
	failureCopy,
	failureFromOperation,
	isFailedOperation,
	isInFlight,
	type ClientVerdict,
	type UpdateOperation,
} from './model';
import { readVersionWithinFiveSeconds, versionHashesAgree } from './versionProbe';
import { hasId, releaseNotesFor, rememberApprovedNotes, rememberId, snapshotReleaseNotes } from './releaseNotes';

export { releaseNotesFor };
export type { UpdateReleaseNotes } from './releaseNotes';

const BASE = '';
const POLL_MS = 1_000;
const HANDLED_KEY = 'corsia:update:handled-operation-ids';
const NOTICED_KEY = 'corsia:update:noticed-target-versions';

import type { UpdateSchedule, UpdateStatus, UpdateSurface } from './types';

export type { UpdateSchedule, UpdateStatus, UpdateSurface } from './types';

export const updateStatus = writable<UpdateStatus | null>(null);
export const updateSurface = writable<UpdateSurface>('closed');
export const updateUnreachable = writable(false);
/** A problem this client found that no stored operation holds; it wins over the operation's own failure copy. */
export const updateFailureVerdict = writable<ClientVerdict | null>(null);
export const updateStarting = writable(false);
/** Offset between this browser clock and the status response's server clock. */
export const updateServerClockOffsetMs = writable(0);
export const updateOperation = derived(updateStatus, ($status) => $status?.operation ?? null);
export const updateInFlight = derived(updateOperation, ($operation) => isInFlight($operation));
/** The server is down or coming back: the composer cannot send. */
export const updateRestarting = derived(
	[updateOperation, updateUnreachable],
	([$operation, $unreachable]) => isInFlight($operation) && ($unreachable || $operation?.phase === 'restarting'),
);

let pollTimer: ReturnType<typeof setInterval> | null = null;
let versionPollTimer: ReturnType<typeof setInterval> | null = null;
let terminalCheckRunning = false;
let versionMismatchStartedAt: number | null = null;
let noticeToastId: string | null = null;

function stopStatusPolling(): void {
	if (pollTimer !== null) clearInterval(pollTimer);
	pollTimer = null;
}

function stopVersionPolling(): void {
	if (versionPollTimer !== null) clearInterval(versionPollTimer);
	versionPollTimer = null;
}

function pollStatusWhileRunning(): void {
	if (pollTimer !== null || !get(updateInFlight)) return;
	pollTimer = setInterval(() => { void loadUpdateStatus(); }, POLL_MS);
}

async function observeRecovery(): Promise<void> {
	const version = await readVersionWithinFiveSeconds();
	if (!version) return;
	updateUnreachable.set(false);
	stopVersionPolling();
	await loadUpdateStatus();
}

function pollVersionWhileUnreachable(): void {
	if (versionPollTimer !== null) return;
	versionPollTimer = setInterval(() => { void observeRecovery(); }, POLL_MS);
	void observeRecovery();
}

function dismissNotice(): void {
	if (noticeToastId) toasts.dismiss(noticeToastId);
	noticeToastId = null;
}

function showNotice(status: UpdateStatus): void {
	const version = status.target_version;
	if (!status.available || isInFlight(status.operation) || status.operation?.target_version === version || !version || !rememberId(NOTICED_KEY, version)) return;
	noticeToastId = toasts.showAction('An update is ready to install.', {
		label: 'View',
		run: openUpdateSheet,
	});
}

function showTerminal(operation: UpdateOperation): void {
	if (!rememberId(HANDLED_KEY, operation.operation_id, 'browser')) return;
	if (operation.status === 'complete') {
		const version = operation.target_version ?? get(updateStatus)?.target_version ?? 'the latest version';
		toasts.pin('update', {
			type: 'success',
			message: `Updated to ${version}`,
			action: { label: "See what's new", run: openUpdateSheet },
		});
		return;
	}
	if (isFailedOperation(operation) && get(updateSurface) === 'closed') {
		const copy = failureCopy(failureFromOperation(operation));
		toasts.pin('update', {
			type: 'error', message: copy.title, detail: copy.toastLine ?? undefined,
			action: { label: 'Details', run: openUpdateSheet },
		});
	}
}

async function verifyComplete(operation: UpdateOperation): Promise<void> {
	if (terminalCheckRunning) return;
	terminalCheckRunning = true;
	try {
		const version = await readVersionWithinFiveSeconds();
		if (!version) {
			setTimeout(() => { void loadUpdateStatus(); }, POLL_MS);
			return;
		}
		if (versionHashesAgree(version)) {
			versionMismatchStartedAt = null;
			showTerminal(operation);
			return;
		}
		versionMismatchStartedAt ??= Date.now();
		if (Date.now() - versionMismatchStartedAt < 10_000) {
			setTimeout(() => { void loadUpdateStatus(); }, POLL_MS);
			return;
		}
		rememberId(HANDLED_KEY, operation.operation_id, 'browser');
		updateFailureVerdict.set({ kind: 'version_mismatch', message: 'The page, the backend and the checkout are not on the same version.' });
		toasts.pin('update', { type: 'error', message: "The update didn't finish", action: { label: 'Details', run: openUpdateSheet } });
	} finally {
		terminalCheckRunning = false;
	}
}

function reconcileOperation(status: UpdateStatus): void {
	const operation = status.operation ?? null;
	if (isInFlight(operation)) {
		updateFailureVerdict.set(null);
		pollStatusWhileRunning();
		return;
	}
	stopStatusPolling();
	stopVersionPolling();
	if (operation?.status === 'complete') {
		if (!hasId(HANDLED_KEY, operation.operation_id, 'browser')) void verifyComplete(operation);
	} else if (operation && isFailedOperation(operation)) showTerminal(operation);
}

/** Read the durable operation projection and start polling only while it is active. */
export async function loadUpdateStatus(refresh = false): Promise<void> {
	const authToken = get(token);
	if (!authToken) return;
	try {
		const response = await fetch(`${BASE}/api/system/update-status?refresh=${refresh ? 'true' : 'false'}`, {
			headers: { Authorization: `Bearer ${authToken}` },
			cache: 'no-store',
		});
		if (!response.ok) throw new Error(`Update check returned ${response.status}`);
		const status = await response.json() as UpdateStatus;
		updateStatus.set(status);
		if (isInFlight(status.operation)) snapshotReleaseNotes(status);
		const serverTime = status.server_time ? Date.parse(status.server_time) : Number.NaN;
		if (!Number.isNaN(serverTime)) updateServerClockOffsetMs.set(serverTime - Date.now());
		updateUnreachable.set(false);
		showNotice(status);
		reconcileOperation(status);
	} catch (error) {
		const previous = get(updateStatus);
		if (isInFlight(previous?.operation)) {
			updateUnreachable.set(true);
			stopStatusPolling();
			pollVersionWhileUnreachable();
			return;
		}
		updateStatus.update((current) => ({
			branch: current?.branch ?? null,
			current_sha: current?.current_sha ?? null,
			remote_sha: current?.remote_sha ?? null,
			commits_behind: null,
			available: null,
			checked_at: current?.checked_at ?? null,
			error: error instanceof Error ? error.message : 'Update check failed.',
			current_version: current?.current_version ?? null,
			target_version: current?.target_version ?? null,
			changes: current?.changes ?? null,
			drain_cap_seconds: current?.drain_cap_seconds ?? null,
			usual_seconds: current?.usual_seconds ?? null,
			agents_working: current?.agents_working ?? null,
			operation: current?.operation ?? null,
			schedule: current?.schedule ?? null,
		}));
	}
}

function showScheduleSaveError(patch: Partial<Pick<UpdateSchedule, 'automatic' | 'time'>> & { tonight?: boolean }): void {
	toasts.showAction("Couldn't save the schedule", {
		label: 'Try again', run: () => { void saveUpdateSchedule(patch); },
	});
}

export async function saveUpdateSchedule(patch: Partial<Pick<UpdateSchedule, 'automatic' | 'time'>> & { tonight?: boolean }): Promise<boolean> {
	const authToken = get(token);
	if (!authToken) {
		showScheduleSaveError(patch);
		return false;
	}
	try {
		const response = await fetch(`${BASE}/api/system/update-schedule`, {
			method: 'PUT', headers: { Authorization: `Bearer ${authToken}`, 'Content-Type': 'application/json' },
			body: JSON.stringify(patch), cache: 'no-store',
		});
		if (!response.ok) throw new Error(`Schedule save returned ${response.status}`);
		const schedule = await response.json() as UpdateSchedule;
		updateStatus.update((current) => current ? { ...current, schedule } : current);
		return true;
	} catch {
		showScheduleSaveError(patch);
		return false;
	}
}

/** Open the one update surface at its decision or last-terminal state. */
export function openUpdateSheet(): void {
	dismissNotice();
	updateSurface.set('sheet');
}

/** Open the update panel without creating a second source of operation truth. */
export function openUpdatePanel(): void {
	dismissNotice();
	updateSurface.set('panel');
}

/** Hide the surface while the operation and its pinned outcome continue independently. */
export function closeUpdateSurface(): void {
	updateSurface.set('closed');
}

/** Start the approved update lifecycle, then immediately adopt its durable record. */
export async function startUpdate(): Promise<void> {
	updateFailureVerdict.set(null);
	const previousOperationId = get(updateStatus)?.operation?.operation_id;
	rememberApprovedNotes(get(updateStatus));
	updateStarting.set(true);
	// The request answers only once the update is built; adopt the operation record meanwhile so the surface shows progress.
	const adopt = setInterval(() => { void loadUpdateStatus(); }, POLL_MS);
	const result = await restartSystem('update').finally(() => { clearInterval(adopt); updateStarting.set(false); });
	if (!result.ok) {
		await loadUpdateStatus(true);
		const latest = get(updateStatus)?.operation;
		// Only a failure this request recorded is its outcome; an older failed row is history.
		if (isFailedOperation(latest) && latest.operation_id !== previousOperationId) {
			openUpdateSheet();
			return;
		}
		const message = result.message ?? "Update didn't start.";
		updateFailureVerdict.set({ kind: 'refused', message });
		if (get(updateSurface) === 'closed') toasts.pin('update', { type: 'error', message: "Update didn't start", detail: message, action: { label: 'Details', run: openUpdateSheet } });
		openUpdateSheet();
		return;
	}
	openUpdatePanel();
	await loadUpdateStatus(true);
}

/** Retry is the same user-authorized update action, after the failure's named fix. */
export async function retryUpdate(): Promise<void> {
	await startUpdate();
}

/** Reset module state between isolated store tests. */
export function resetUpdateStoreForTests(): void {
	stopStatusPolling();
	stopVersionPolling();
	terminalCheckRunning = false;
	versionMismatchStartedAt = null;
	noticeToastId = null;
	updateStatus.set(null);
	updateSurface.set('closed');
	updateUnreachable.set(false);
	updateFailureVerdict.set(null);
	updateStarting.set(false);
	updateServerClockOffsetMs.set(0);
}
