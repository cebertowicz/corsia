/** What a failed update says: one plain sentence per cause, and what state CORSIA is in. */

import type { ClientVerdict, UpdateFailure, UpdateOperation } from './model';

function parseDetail(detail: string | null | undefined): Record<string, unknown> {
	try {
		const parsed = JSON.parse(detail ?? '{}');
		return parsed && typeof parsed === 'object' ? parsed : {};
	} catch {
		return {};
	}
}

export function diskNumbers(detail: string | null | undefined): { neededGb: number; freeGb: number } | null {
	const parsed = parseDetail(detail);
	const needed = Number(parsed.needed_bytes);
	const free = Number(parsed.free_bytes);
	if (!Number.isFinite(needed) || !Number.isFinite(free) || needed <= 0) return null;
	return { neededGb: Math.ceil(needed / 1e9), freeGb: Math.max(0, Math.floor(free / 1e9)) };
}

export function failureFromOperation(op: UpdateOperation): UpdateFailure {
	return {
		code: op.failure_code ?? null,
		detail: op.failure_detail ?? null,
		summary: op.error ?? null,
		conductorTold: Boolean(op.conductor_told_at),
		log: op.log_tail ?? null,
	};
}

export function failureFromVerdict(client: ClientVerdict): UpdateFailure {
	return { code: null, detail: null, summary: null, conductorTold: false, log: null, client };
}

export interface FailureCopy {
	/** The panel's header and the toast's first line. */
	title: string;
	/** The one plain sentence naming the cause. */
	headline: string;
	/** What state CORSIA is in, with the figures a refusal carries. */
	body: string;
	/** The toast's second line: the number or fact to act on. */
	toastLine: string | null;
	/** The action's label, named for the fix; null when no action by the person can clear it. */
	retryLabel: string | null;
	/** `restart` when the person's next step is restarting CORSIA, not asking for another update. */
	retryAction?: 'restart';
}

// Only where nothing was touched: the disk check and a download that never advanced the checkout.
const RAN_ON = 'CORSIA kept running and nothing was changed.';
// The checkout may already hold the new version, so this claims only what is certain.
const KEPT_VERSION = 'CORSIA is still running the version it had.';

function refusal(headline: string, body: string, retryLabel: string | null): FailureCopy {
	return { title: "Update didn't start", headline, body, toastLine: null, retryLabel };
}

function stopped(headline: string, body: string): FailureCopy {
	return refusal(headline, body, 'Try again');
}

function diskCopy(detail: string | null): FailureCopy {
	const disk = diskNumbers(detail);
	const body = disk ? `Needs ${disk.neededGb} GB free; this computer has ${disk.freeGb} GB. ${RAN_ON}` : RAN_ON;
	return {
		title: "Update didn't start",
		headline: 'Not enough disk space for a safety backup.',
		body,
		toastLine: disk ? `Needs ${Math.max(1, disk.neededGb - disk.freeGb)} GB more free space` : null,
		retryLabel: 'Try again after freeing space',
	};
}

/** One plain sentence per failure code; the stored summary when there is no code. */
export function failureCopy(failure: UpdateFailure): FailureCopy {
	if (failure.client?.kind === 'version_mismatch') {
		return {
			title: "The update didn't finish",
			headline: 'The update finished, but CORSIA still shows the old version.',
			body: failure.client.message,
			toastLine: null,
			retryLabel: null,
		};
	}
	if (failure.client) {
		return {
			title: "Update didn't start",
			headline: failure.client.message,
			body: '',
			toastLine: null,
			retryLabel: 'Try again',
		};
	}
	switch (failure.code) {
		case 'disk_space':
			return diskCopy(failure.detail);
		case 'fetch_failed':
			return {
				title: "Update didn't start",
				headline: "Couldn't download the update.",
				body: RAN_ON,
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'build_failed':
			return {
				title: "Update didn't start",
				headline: "Couldn't build the update.",
				body: KEPT_VERSION,
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'launch_failed':
			return {
				title: "Update didn't finish",
				headline: "Couldn't start the restart.",
				body: 'CORSIA is still running.',
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'drain_cancelled':
			return {
				title: 'Update cancelled',
				headline: 'The update was cancelled before CORSIA restarted.',
				body: KEPT_VERSION,
				toastLine: null,
				retryLabel: 'Try again',
			};
		case 'drain_failed':
			return stopped("Your agents didn't reach a safe stopping point in time.", KEPT_VERSION);
		case 'update_timed_out':
			return stopped('The update took too long to download and build.', KEPT_VERSION);
		case 'prepare_failed':
			return stopped("Couldn't prepare the update.", KEPT_VERSION);
		case 'index_locked':
			return refusal(
				"Another program is using CORSIA's files.",
				`Wait for it to finish, then try again. If it doesn't clear, ask the CORSIA agent to remove the leftover lock. ${RAN_ON}`,
				'Try again',
			);
		case 'checkout_detached':
			return refusal("This copy of CORSIA can't update itself because it isn't set up to follow updates.", `Ask the CORSIA agent to switch it back. ${RAN_ON}`, null);
		case 'checkout_dirty':
			return refusal('This copy of CORSIA has local changes.', `Save or discard them, then try again. ${RAN_ON}`, 'Try again');
		case 'restart_pending':
			return { ...refusal('An update is ready and waiting for a restart.', `Restart CORSIA to finish it. ${KEPT_VERSION}`, 'Restart now'), retryAction: 'restart' };
		default:
			// A terminal row carries no phase, so nothing here can say how far the update got.
			return {
				title: "Update didn't finish",
				headline: 'The update stopped before it finished.',
				body: '',
				toastLine: null,
				retryLabel: 'Try again',
			};
	}
}
