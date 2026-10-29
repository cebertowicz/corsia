import { derived, get, writable } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { setupMode } from '$lib/stores/setupMode';
import { toasts } from '$lib/stores/toast';
import { requestSettingsSection } from '$lib/stores/settingsNavigation';
import { restartSystem } from '$lib/stores/settingsSystemRestart';
import { failureCopy, failureFromOperation } from './failureCopy';
import { isFailedOperation, isInFlight, isResolvedFailure, type ClientVerdict, type UpdateChanges, type UpdateOperation } from './model';
import { markOutcomeSeen } from './outcome';
import { readVersionWithinFiveSeconds, versionHashesAgree } from './versionProbe';
import { clearStartupProgress, refreshStartupProgress } from './startupProgress';
import type { UpdateSchedule, UpdateStatus } from './types';

const BASE = '';
const POLL_MS = 1_000;
export type { UpdateSchedule, UpdateStatus } from './types';
export const updateStatus = writable<UpdateStatus | null>(null);
/** The person shrank the progress toast to its pill; it lasts until the operation settles. */
export const updateProgressMinimized = writable(false);
export const updateUnreachable = writable(false);
/** A problem this client found that no stored operation holds; it wins over the operation's own failure copy. */
export const updateFailureVerdict = writable<ClientVerdict | null>(null);
export const updateStarting = writable(false);
/** Offset between this browser clock and the status response's server clock. */
export const updateServerClockOffsetMs = writable(0);
export const updateConfirmed = writable<{ operationId: string; version: string | null } | null>(null);
export const updateToastSuppressed = writable(false);
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
let latestNoticeStatus: UpdateStatus | null = null;
// What this page already showed. The durable record of what any device showed is the operation's `outcome_seen_at`.
const shownOutcomes = new Set<string>();
const noticedVersions = new Set<string>();
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
	clearStartupProgress();
	await loadUpdateStatus();
}

function pollVersionWhileUnreachable(): void {
	if (versionPollTimer !== null) return;
	versionPollTimer = setInterval(() => { void observeRecovery(); void refreshStartupProgress(); }, POLL_MS);
	void observeRecovery();
	void refreshStartupProgress();
}

function dismissNotice(forSuppression = false): void {
	const visible = noticeToastId && get(toasts).some((toast) => toast.id === noticeToastId);
	if (forSuppression && visible && latestNoticeStatus?.target_version) noticedVersions.delete(latestNoticeStatus.target_version);
	if (noticeToastId) toasts.dismiss(noticeToastId);
	noticeToastId = null;
}

function showNotice(status: UpdateStatus): void {
	latestNoticeStatus = status;
	const version = status.target_version;
	if (get(updateToastSuppressed) || get(setupMode) === 'flow') {
		dismissNotice();
		return;
	}
	if (!status.available || isInFlight(status.operation) || status.operation?.target_version === version || !version || noticedVersions.has(version)) return;
	noticedVersions.add(version);
	noticeToastId = toasts.showButton('Update ready. Open Updates', openUpdates);
}

derived([updateToastSuppressed, setupMode], ([$suppressed, $setupMode]) => $suppressed || $setupMode === 'flow')
	.subscribe((suppressed) => {
		if (suppressed) dismissNotice(true);
		else if (latestNoticeStatus) showNotice(latestNoticeStatus);
	});

/** Remove the progress toast; a tab that reads a terminal status second must never keep a spinner. */
function dropProgressToast(): void {
	if (get(toasts).find((toast) => toast.pinned?.key === 'update')?.type === 'progress') toasts.unpin('update');
}

/** Show an unseen outcome once per page. A success is seen once shown; a failure only when someone dismisses or retries it. */
function showTerminal(operation: UpdateOperation): void {
	if (shownOutcomes.has(operation.operation_id)) return;
	shownOutcomes.add(operation.operation_id);
	if (operation.status === 'complete') {
		const version = operation.target_version ?? get(updateStatus)?.target_version ?? 'the latest version';
		const overnight = operation.trigger === 'tonight' || operation.trigger === 'automatic';
		toasts.pin('update', {
			type: 'success',
			message: overnight ? `Updated overnight to ${version}` : `Updated to ${version}`,
			action: { label: operation.changes?.kind === 'commits' ? 'See changes' : "What's new", run: openUpdates },
		});
		void markOutcomeSeen(operation.operation_id);
		return;
	}
	if (isFailedOperation(operation)) {
		const copy = failureCopy(failureFromOperation(operation));
		toasts.pin('update', {
			type: 'error', message: copy.title, detail: copy.toastLine ?? undefined,
			action: { label: 'Details', run: openUpdates },
			onClose: () => { void markOutcomeSeen(operation.operation_id); },
		});
	}
}

async function verifyComplete(operation: UpdateOperation): Promise<void> {
	if (terminalCheckRunning || shownOutcomes.has(operation.operation_id)) return;
	terminalCheckRunning = true;
	try {
		const version = await readVersionWithinFiveSeconds();
		if (!version) {
			setTimeout(() => { void loadUpdateStatus(); }, POLL_MS);
			return;
		}
		if (versionHashesAgree(version)) {
			versionMismatchStartedAt = null;
			updateConfirmed.set({ operationId: operation.operation_id, version: operation.target_version ?? get(updateStatus)?.target_version ?? null });
			showTerminal(operation);
			void loadUpdateStatus();
			return;
		}
		versionMismatchStartedAt ??= Date.now();
		if (Date.now() - versionMismatchStartedAt < 10_000) {
			setTimeout(() => { void loadUpdateStatus(); }, POLL_MS);
			return;
		}
		shownOutcomes.add(operation.operation_id);
		void markOutcomeSeen(operation.operation_id);
		updateFailureVerdict.set({ kind: 'version_mismatch', message: 'The page, the backend and the checkout are not on the same version.' });
		toasts.pin('update', { type: 'error', message: "The update didn't finish", action: { label: 'Details', run: openUpdates } });
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
	dropProgressToast();
	updateProgressMinimized.set(false);
	if (!operation || operation.outcome_seen_at || isResolvedFailure(operation)) return;
	if (operation.status === 'complete') void verifyComplete(operation);
	else if (isFailedOperation(operation)) showTerminal(operation);
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
		label: 'Retry', run: () => { void saveUpdateSchedule(patch); },
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

/** Open Settings > System at the Updates card; Settings keeps the thread it opened over, so Back returns to it. */
export function openUpdates(): void {
	dismissNotice();
	requestSettingsSection('system', 'update');
}

/** Start the approved update lifecycle, then immediately adopt its durable record. */
export async function startUpdate(): Promise<void> {
	updateFailureVerdict.set(null);
	const previous = get(updateStatus)?.operation;
	const previousOperationId = previous?.operation_id;
	updateStarting.set(true);
	// Retrying is acting on the failure: it no longer needs to greet any device.
	if (isFailedOperation(previous)) void markOutcomeSeen(previous.operation_id);
	// The request answers only once the update is built; adopt the operation record meanwhile so the surface shows progress.
	const adopt = setInterval(() => { void loadUpdateStatus(); }, POLL_MS);
	const result = await restartSystem('update').finally(() => { clearInterval(adopt); updateStarting.set(false); });
	if (!result.ok) {
		await loadUpdateStatus(true);
		const latest = get(updateStatus)?.operation;
		// Only a failure this request recorded is its outcome; an older failed row is history.
		// The failed row surfaces itself through the poll; nothing more to show here.
		if (isFailedOperation(latest) && latest.operation_id !== previousOperationId) return;
		const message = result.message ?? "Update didn't start.";
		updateFailureVerdict.set({ kind: 'refused', message });
		toasts.pin('update', { type: 'error', message: "Update didn't start", detail: message, action: { label: 'Details', run: openUpdates } });
		return;
	}
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
	shownOutcomes.clear();
	noticedVersions.clear();
	updateStatus.set(null);
	updateProgressMinimized.set(false);
	updateUnreachable.set(false);
	updateFailureVerdict.set(null);
	updateStarting.set(false);
	updateServerClockOffsetMs.set(0);
	updateConfirmed.set(null);
	updateToastSuppressed.set(false);
	latestNoticeStatus = null;
}
