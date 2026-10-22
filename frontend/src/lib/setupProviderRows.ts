import type { AiRow } from './components/SetupAiRows.svelte';
import type { ModelSource } from './conductorSources';
import { LOW_MEMORY_LOCAL_MODEL_MESSAGE, type LocalPick } from './setupLocalPick.svelte';
import { measuredDownloadTime } from './setupLocalPresentation';

// Settings imports the separable first sentence from setupLocalPick.
export type LocalRowState = ModelSource;
export type LocalRowInput = {
	source?: LocalRowState; pick: LocalPick | null; eligible: boolean; downloading: boolean; ready: boolean; startError: string; startFix?: string; startReference?: string; freeDisk?: number;
	/** A cloud model works, so the local one is no longer the only way through setup and may be turned off. */
	cloudReady?: boolean;
	/** The person turned the switch off mid-download and is being asked to confirm. */
	confirmingStop?: boolean;
	/** Turning the Ready model off failed; the reference the service logged it under, if any. */
	turnOffFailed?: { reference?: string };
	start: () => void; stop: () => void; check: () => void;
	confirmStop?: (asking: boolean) => void; turnOff?: () => void;
};

const LOCAL_FAILURE_STATES = new Set(['failed', 'setup_failed', 'start_failed']);
export const LOCAL_HELD_DOWNLOADING = 'CORSIA is downloading this so setup can continue. To turn it off, connect a cloud provider first.';
export const LOCAL_HELD_READY = 'CORSIA needs this to continue setup. To turn it off, connect a cloud provider first.';
export const LOCAL_TURN_OFF_FAILED = 'CORSIA couldn’t turn off the local model. Turn it off again, or save a report.';

/** How much of the download is on disk, in GB, or null when the size or progress is unknown. */
function downloadedGb(sizeGb: number | null, percent?: number | null): number | null {
	if (sizeGb === null || !Number.isFinite(sizeGb) || percent === null || percent === undefined) return null;
	return Math.round(sizeGb * percent / 10) / 10;
}

/** Present one local lifecycle and one cause-specific recovery action. */
export function setupLocalRow(input: LocalRowInput): AiRow {
	const { source, pick, eligible, downloading, ready, startError, startFix, startReference, freeDisk, cloudReady = false, confirmingStop = false, turnOffFailed, start, stop, check, confirmStop, turnOff } = input;
	const row = { key: 'omlx', name: 'Local model' };
	const model = pick?.model?.name ?? source?.settings_download?.name ?? 'Local model';
	const size = pick?.model ? `${pick.model.download_size_gb} GB` : source?.download_size ?? '';
	if (!eligible || pick?.state === 'low_memory') return { ...row, meta: LOW_MEMORY_LOCAL_MODEL_MESSAGE };
	if (ready) {
		const failed = cloudReady && turnOffFailed;
		return { ...row, meta: `${model} · runs on this computer`, pill: { state: 'success', label: 'Ready' },
			detail: failed ? LOCAL_TURN_OFF_FAILED : cloudReady ? undefined : LOCAL_HELD_READY, error: Boolean(failed), reference: failed ? turnOffFailed.reference : undefined, reportable: Boolean(failed),
			toggle: { on: true, held: !cloudReady, run: (next) => { if (!next) turnOff?.(); } } };
	}
	if (downloading) {
		if (source?.local_state === 'starting') return { ...row, meta: 'Starting the local model…', busy: true };
		const meta = source?.download_stalled ? 'Download paused. It continues when the connection returns.'
			: [model, size, `Downloading ${source?.download_percent ?? 0}%`, measuredDownloadTime(source?.download_eta_seconds)].filter(Boolean).join(' · ');
		const toggle = { on: !confirmingStop, held: !cloudReady, run: (next: boolean) => { if (!next) confirmStop?.(true); } };
		if (!cloudReady) return { ...row, meta, detail: LOCAL_HELD_DOWNLOADING, toggle };
		if (confirmingStop) {
			const gb = downloadedGb(pick?.model ? pick.model.download_size_gb : source?.download_size ? Number.parseFloat(source.download_size) : null, source?.download_percent);
			return { ...row, meta, toggle, detail: `Stop downloading the local chat model? ${gb === null ? 'Everything' : `The ${gb} GB`} downloaded so far is deleted.`,
				confirm: [{ label: 'Stop download', run: () => { confirmStop?.(false); stop(); } }, { label: 'Keep downloading', run: () => confirmStop?.(false) }] };
		}
		return { ...row, meta, toggle };
	}
	// Downloaded, and its server was turned off: the switch turns it back on.
	if (source?.reason_code === 'not_running' && source.local_state === 'stopped') {
		return { ...row, meta: `${model} · Off`, toggle: { on: false, run: (next) => { if (next) start(); } } };
	}
	// The download finished: the model is being listed and loaded, never offered for download again;
	// Check again asks the service now rather than waiting on the next refresh.
	if (source?.local_state === 'ready') return { ...row, meta: `${model} · Finishing setup…`, busy: true, spinning: true, action: { label: 'Check again', run: check } };
	if (pick?.model && freeDisk !== undefined && freeDisk < pick.model.download_size_gb) return { ...row, meta: `Needs ${size} free, and this computer has ${freeDisk} GB. Free up space, then check again.`, action: { label: 'Check again', run: check } };
	if (LOCAL_FAILURE_STATES.has(source?.local_state ?? '') || startError) {
		const cause = source?.setup_error_code ?? startError;
		const reference = source?.setup_error_reference ?? startReference;
		const meta = startError ? startFix ?? '' : source?.fix ?? '';
		return { ...row, meta, reference, error: true, reportable: Boolean(startError) || cause === 'other' || cause === undefined || source?.local_state === 'start_failed' || cause === 'start_failed' || source?.local_state === 'setup_failed' || cause === 'setup_failed', action: { label: 'Retry', run: start } };
	}
	if (pick?.state === 'failed') {
		const message = pick.cause === 'memory' ? 'Couldn’t find a local model that fits this computer. Try again.'
			: pick.cause === 'own_omlx_upgrade_required' ? 'The oMLX on this computer is out of date or broken. Update it, then try again.'
			: 'CORSIA couldn’t set up the local model.';
		return { ...row, meta: message, reference: pick.ref, error: true, reportable: true, action: { label: 'Retry', run: check } };
	}
	return { ...row, meta: `Free and private. Runs on this computer, even offline.${size ? ` ${size} download.` : ''}`, action: { label: 'Download', run: start } };
}

/** The footer follows the working path, independently of a background download. */
export function setupStartNote(ready: boolean, eligible: boolean, downloading: boolean, eta?: number | null): string {
	if (ready) return downloading ? 'The local model keeps downloading. Follow it in Settings › Providers.' : '';
	if (!eligible) return 'Set up a subscription or add an API provider to continue.';
	if (downloading) return `Ready when the local model finishes downloading${measuredDownloadTime(eta) ? `, in ${measuredDownloadTime(eta)}` : ''}.`;
	return 'Download the local model, set up a subscription or add an API provider to continue.';
}
