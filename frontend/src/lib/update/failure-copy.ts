import type { UpdateFailure } from './model';

export interface FailureCopy {
	/** The panel's header and the toast's first line. */
	title: string;
	/** The one plain sentence naming the cause. */
	headline: string;
	/** What state CORSIA is in, with the figures a refusal carries. */
	body: string;
	/** The toast's second line: the number or fact to act on. */
	toastLine: string | null;
	/** The retry's label, named for the fix. */
	retryLabel: string | null;
}

// Only where nothing was touched: the disk check and a download that never advanced the checkout.
const RAN_ON = 'CORSIA kept running and nothing was changed.';
// The checkout may already hold the new version, so this claims only what is certain.
const KEPT_VERSION = 'CORSIA is still running the version it had.';

function diskNumbers(detail: string | null | undefined): { neededGb: number; freeGb: number } | null {
	try {
		const parsed = JSON.parse(detail ?? '{}');
		if (!parsed || typeof parsed !== 'object') return null;
		const values = parsed as Record<string, unknown>;
		const needed = Number(values.needed_bytes);
		const free = Number(values.free_bytes);
		if (!Number.isFinite(needed) || !Number.isFinite(free) || needed <= 0) return null;
		return { neededGb: Math.ceil(needed / 1e9), freeGb: Math.max(0, Math.floor(free / 1e9)) };
	} catch {
		return null;
	}
}

function diskCopy(detail: string | null): FailureCopy {
	const disk = diskNumbers(detail);
	const body = disk ? `Needs ${disk.neededGb} GB free; this Mac has ${disk.freeGb} GB. ${RAN_ON}` : RAN_ON;
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
			headline: 'The update finished, but the running versions do not match.',
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
