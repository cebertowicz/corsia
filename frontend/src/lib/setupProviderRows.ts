import type { AiRow } from './components/SetupAiRows.svelte';
import type { ModelSource } from './conductorSources';
import { LOW_MEMORY_LOCAL_MODEL_MESSAGE, type LocalPick } from './setupLocalPick.svelte';
import { measuredDownloadTime } from './setupLocalPresentation';

// Settings imports the separable first sentence from setupLocalPick.
export type LocalRowState = ModelSource & { local_state?: string; setup_error_code?: string; setup_error_reference?: string; bytes_to_free?: number };
export type LocalRowInput = { source?: LocalRowState; pick: LocalPick | null; eligible: boolean; downloading: boolean; ready: boolean; startError: string; startReference?: string; freeDisk?: number; start: () => void; stop: () => void; check: () => void };

type LocalSetupFailure = { cause?: string; outcome?: string; reference?: string; bytesToFree?: number; size?: string };

/** Keep local setup recovery specific without exposing installer output. */
function withReference(message: string, reference?: string): string {
	return `${message}${reference ? ` Reference: ${reference}` : ''}`;
}

export function localSetupRecovery({ cause, outcome, reference, bytesToFree, size }: LocalSetupFailure): string {
	if (cause === 'own_omlx_upgrade_required') return withReference('Update your oMLX, then Retry.', reference);
	if (outcome === 'setup_failed' || cause === 'setup_failed') return withReference('CORSIA couldn’t set up the local model. Retry, or save a report.', reference);
	if (outcome === 'start_failed' || cause === 'start_failed') return withReference('The local model downloaded but couldn’t start. Retry, or save a report.', reference);
	if (cause === 'disk_full') return `Couldn’t download the local model: the disk is full. Free up ${bytesToFree ? `${Math.ceil(bytesToFree / 1e9)} GB` : size}, then Retry.`;
	if (cause === 'connection') return 'Couldn’t download the local model because the connection was lost. Check your internet connection, then Retry.';
	if (cause === 'memory') return 'This computer doesn’t have enough memory to run the local model. Choose a subscription or API provider instead.';
	if (cause === 'other_omlx') return 'Another oMLX is running. Quit your other oMLX, then Retry.';
	if (cause === 'other') return withReference('CORSIA couldn’t set up the local model. Retry.', reference);
	return withReference('CORSIA couldn’t set up the local model. Retry.', reference);
}

/** Present one local lifecycle and one cause-specific recovery action. */
export function setupLocalRow(input: LocalRowInput): AiRow {
	const { source, pick, eligible, downloading, ready, startError, startReference, freeDisk, start, stop, check } = input;
	const row = { key: 'omlx', name: 'Local model' };
	const model = pick?.model?.name ?? source?.settings_download?.name ?? 'Local model';
	const size = pick?.model ? `${pick.model.download_size_gb} GB` : source?.download_size ?? '';
	if (!eligible) return { ...row, meta: LOW_MEMORY_LOCAL_MODEL_MESSAGE };
	if (ready) return { ...row, meta: `${model} · runs on this computer`, pill: { state: 'success', label: 'Ready' } };
	if (downloading) {
		if (source?.download_stalled) return { ...row, meta: 'Download paused. It continues when the connection returns.', action: { label: 'Stop download', run: stop } };
		if (source?.local_state === 'starting' || /load|start/i.test(source?.setup_stage ?? '')) return { ...row, meta: 'Starting the local model…', busy: true };
		return { ...row, meta: [model, size, `Downloading ${source?.download_percent ?? 0}%`, measuredDownloadTime(source?.download_eta_seconds)].filter(Boolean).join(' · '), action: { label: 'Stop download', run: stop } };
	}
	if (pick?.model && freeDisk !== undefined && freeDisk < pick.model.download_size_gb) return { ...row, meta: `Needs ${size} free, and this computer has ${freeDisk} GB. Free up space, then check again.`, action: { label: 'Check again', run: check } };
	if (source?.setup_error || startError) {
		const cause = source?.setup_error_code ?? startError;
		const outcome = source?.local_state;
		const meta = localSetupRecovery({ cause, outcome, reference: source?.setup_error_reference ?? startReference, bytesToFree: source?.bytes_to_free, size });
		return { ...row, meta, error: true, reportable: Boolean(startError) || cause === 'other' || cause === undefined || outcome === 'start_failed' || cause === 'start_failed' || outcome === 'setup_failed' || cause === 'setup_failed', action: { label: 'Retry', run: start } };
	}
	if (pick?.state === 'failed') {
		const message = pick.cause === 'memory' ? 'Couldn’t find a local model that fits this computer. Retry.'
			: pick.cause === 'own_omlx_upgrade_required' ? 'Update your oMLX, then Retry.'
			: 'CORSIA couldn’t choose a local model. Retry, or save a report.';
		return { ...row, meta: withReference(message, pick.ref), error: true, reportable: true, action: { label: 'Retry', run: check } };
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
