import type { AiRow } from './components/SetupAiRows.svelte';
import type { ModelSource } from './conductorSources';
import { LOW_MEMORY_LOCAL_MODEL_MESSAGE, type LocalPick } from './setupLocalPick.svelte';
import { measuredDownloadTime } from './setupLocalPresentation';

// Settings imports the separable first sentence from setupLocalPick.
export type LocalRowState = ModelSource & { local_state?: string; setup_error_code?: string; setup_error_reference?: string; bytes_to_free?: number };
export type LocalRowInput = { source?: LocalRowState; pick: LocalPick | null; eligible: boolean; downloading: boolean; ready: boolean; startError: string; freeDisk?: number; start: () => void; stop: () => void; stopOther: () => void; check: () => void };

/** Present one local lifecycle and one cause-specific recovery action. */
export function setupLocalRow(input: LocalRowInput): AiRow {
	const { source, pick, eligible, downloading, ready, startError, freeDisk, start, stop, stopOther, check } = input;
	const row = { key: 'omlx', name: 'Local model' };
	const model = pick?.model?.name ?? source?.settings_download?.name ?? 'Local model';
	const size = pick?.model ? `${pick.model.download_size_gb} GB` : source?.download_size ?? '';
	if (!eligible) return { ...row, meta: LOW_MEMORY_LOCAL_MODEL_MESSAGE };
	if (ready) return { ...row, meta: `${model} · runs on this computer`, pill: { state: 'success', label: 'Ready' } };
	if (source?.local_state === 'not_loaded') return { ...row, meta: 'The local model downloaded, but there isn’t enough free memory to start it while your oMLX is running.', action: { label: 'Stop mine', run: stopOther } };
	if (downloading) {
		if (source?.download_stalled) return { ...row, meta: 'Download paused. It continues when the connection returns.', action: { label: 'Stop download', run: stop } };
		if (source?.local_state === 'starting' || /load|start/i.test(source?.setup_stage ?? '')) return { ...row, meta: 'Starting the local model…', busy: true };
		return { ...row, meta: [model, size, `Downloading ${source?.download_percent ?? 0}%`, measuredDownloadTime(source?.download_eta_seconds)].filter(Boolean).join(' · '), action: { label: 'Stop download', run: stop } };
	}
	if (pick?.model && freeDisk !== undefined && freeDisk < pick.model.download_size_gb) return { ...row, meta: `Needs ${size} free, and this computer has ${freeDisk} GB. Free up space, then check again.`, action: { label: 'Check again', run: check } };
	if (source?.setup_error || startError) {
		const cause = source?.setup_error_code;
		const reference = source?.setup_error_reference ? ` Reference: ${source.setup_error_reference}` : '';
		const meta = cause === 'disk_full' ? `Couldn’t download the local model: the disk is full. Free up ${source?.bytes_to_free ? `${Math.ceil(source.bytes_to_free / 1e9)} GB` : size}, then retry.`
			: cause === 'start_failed' ? `The local model downloaded but couldn’t start. Retry, or save a report.${reference}`
			: 'Couldn’t download the local model. Check your internet connection, then retry.';
		return { ...row, meta, error: true, action: { label: 'Retry', run: start } };
	}
	if (pick?.state === 'failed') return { ...row, meta: 'Couldn’t find a local model that fits this computer. Retry.', error: true, action: { label: 'Retry', run: check } };
	return { ...row, meta: `Free and private. Runs on this computer, even offline.${size ? ` ${size} download.` : ''}`, action: { label: 'Download', run: start } };
}

/** The footer follows the working path, independently of a background download. */
export function setupStartNote(ready: boolean, eligible: boolean, downloading: boolean, eta?: number | null): string {
	if (ready) return downloading ? 'The local model keeps downloading. Follow it in Settings › Providers.' : '';
	if (!eligible) return 'Set up a subscription or add an API provider to continue.';
	if (downloading) return `Ready when the local model finishes downloading${measuredDownloadTime(eta) ? `, in ${measuredDownloadTime(eta)}` : ''}.`;
	return 'Download the local model or set up a subscription to continue.';
}
