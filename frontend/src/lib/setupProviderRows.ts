import type { AiRow } from './components/SetupAiRows.svelte';
import type { ModelSource } from './conductorSources';
import { LOW_MEMORY_LOCAL_MODEL_MESSAGE, type LocalPick } from './setupLocalPick.svelte';
import { measuredDownloadTime } from './setupLocalPresentation';

// Settings imports the separable first sentence from setupLocalPick.
export type LocalRowState = ModelSource;
export type LocalRowInput = { source?: LocalRowState; pick: LocalPick | null; eligible: boolean; downloading: boolean; ready: boolean; startError: string; startFix?: string; startReference?: string; freeDisk?: number; start: () => void; stop: () => void; check: () => void };

const LOCAL_FAILURE_STATES = new Set(['failed', 'setup_failed', 'start_failed']);

/** Present one local lifecycle and one cause-specific recovery action. */
export function setupLocalRow(input: LocalRowInput): AiRow {
	const { source, pick, eligible, downloading, ready, startError, startFix, startReference, freeDisk, start, stop, check } = input;
	const row = { key: 'omlx', name: 'Local model' };
	const model = pick?.model?.name ?? source?.settings_download?.name ?? 'Local model';
	const size = pick?.model ? `${pick.model.download_size_gb} GB` : source?.download_size ?? '';
	if (!eligible) return { ...row, meta: LOW_MEMORY_LOCAL_MODEL_MESSAGE };
	if (ready) return { ...row, meta: `${model} · runs on this computer`, pill: { state: 'success', label: 'Ready' } };
	if (downloading) {
		if (source?.local_state === 'starting') return { ...row, meta: 'Starting the local model…', busy: true };
		if (source?.download_stalled) return { ...row, meta: 'Download paused. It continues when the connection returns.', action: { label: 'Stop download', run: stop } };
		return { ...row, meta: [model, size, `Downloading ${source?.download_percent ?? 0}%`, measuredDownloadTime(source?.download_eta_seconds)].filter(Boolean).join(' · '), action: { label: 'Stop download', run: stop } };
	}
	if (pick?.model && freeDisk !== undefined && freeDisk < pick.model.download_size_gb) return { ...row, meta: `Needs ${size} free, and this computer has ${freeDisk} GB. Free up space, then check again.`, action: { label: 'Check again', run: check } };
	if (LOCAL_FAILURE_STATES.has(source?.local_state ?? '') || startError) {
		const cause = source?.setup_error_code ?? startError;
		const reference = source?.setup_error_reference ?? startReference;
		const meta = startError ? startFix ?? '' : source?.fix ?? '';
		return { ...row, meta, reference, error: true, reportable: Boolean(startError) || cause === 'other' || cause === undefined || source?.local_state === 'start_failed' || cause === 'start_failed' || source?.local_state === 'setup_failed' || cause === 'setup_failed', action: { label: 'Retry', run: start } };
	}
	if (pick?.state === 'failed') {
		const message = pick.cause === 'memory' ? 'Couldn’t find a local model that fits this computer. Try again.'
			: pick.cause === 'own_omlx_upgrade_required' ? 'Update your oMLX, then try again.'
			: 'CORSIA couldn’t set up the local model. Try again, or save a report.';
		return { ...row, meta: message, reference: pick.ref, error: true, reportable: true, action: { label: 'Retry', run: check } };
	}
	return { ...row, meta: `Free and private. Runs on this computer, even offline.${size ? ` ${size} download.` : ''}`, action: { label: 'Download', run: start } };
}

/** The footer follows the working path, independently of a background download. */
export function setupStartNote(ready: boolean, eligible: boolean, downloading: boolean, eta?: number | null): string {
	if (ready) return downloading ? 'The local model keeps downloading. Follow it in Settings › Providers.' : '';
	if (!eligible) return 'Set up a subscription or add an API provider to continue.';
	if (downloading) return `Ready when the local model finishes downloading${measuredDownloadTime(eta) ? `, in ${measuredDownloadTime(eta)}` : ''}.`;
	return 'Download the local model or set up a subscription to continue.';
}
