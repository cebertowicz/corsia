import type { SetupJob } from './components/SetupShell.svelte';
import { sourceIsReady, type ModelSource } from './conductorSources';

export function remainingTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 90) return 'about 1 min left';
	if (seconds < 90 * 60) return `about ${Math.round(seconds / 60)} min left`;
	return `about ${Math.round(seconds / 3600)} hr left`;
}

export function localJob(source: ModelSource | undefined, downloading: boolean, modelName: string | undefined, page: string, signedIn: boolean, startError: string, onCancel: () => void): SetupJob | undefined {
	if (downloading) {
		const progress = source?.download_percent == null ? undefined : source.download_percent / 100;
		const meta = source?.download_stalled ? 'Download paused' : source?.download_percent == null ? source?.setup_stage || 'Downloading' : [
			`${source.download_percent}%`, remainingTime(source.download_eta_seconds),
		].filter(Boolean).join(' · ');
		return { label: modelName || source?.name || 'Local model', state: 'busy', meta, progress,
			action: page === 'models' && signedIn ? { label: 'Cancel', onclick: onCancel } : undefined };
	}
	return source?.setup_error || startError ? { label: modelName || source?.name || 'Local model', state: 'error', meta: 'Couldn’t download' } : undefined;
}

export function setupFinishNote(source: ModelSource | undefined, downloading: boolean, startError: string): string {
	if (startError || source?.setup_error) return 'The local model couldn’t start. Try again, or choose another source.';
	return downloading ? 'Ready when the download finishes. You can close this window; CORSIA keeps going.' : 'Connect Claude Code or Codex, or add an API key, to continue.';
}

export function setupSourceMeta(source: ModelSource, startError: string, modelSize: string): string {
	if (source.source === 'omlx') {
		if (source.setup_downloading || source.setup_stage) {
			if (source.download_stalled) return 'Download paused';
			const eta = remainingTime(source.download_eta_seconds);
			return source.download_percent == null && source.setup_stage ? source.setup_stage : ['Downloading in the background', eta].filter(Boolean).join(' · ');
		}
		return source.setup_error || startError ? 'Download needs attention' : `Local model · ${modelSize}`;
	}
	if (source.state === 'not_installed') return `${source.name} needs its official app installed first.`;
	if (source.state === 'needs_auth') return 'Sign in to use your subscription';
	return sourceIsReady(source) ? 'Ready to use.' : 'Check this source, then try again.';
}
