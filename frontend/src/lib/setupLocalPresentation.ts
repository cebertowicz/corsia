import type { SetupJob } from './components/SetupShell.svelte';
import { sourceIsReady, type ModelSource } from './conductorSources';
import { cannotCheck } from './ownOmlxCopy';

export function measuredDownloadTime(seconds: number | null | undefined): string | undefined {
	if (seconds == null || seconds < 0) return undefined;
	if (seconds < 90) return 'about 1 min';
	if (seconds < 90 * 60) return `about ${Math.round(seconds / 60)} min`;
	return `about ${Math.round(seconds / 3600)} hr`;
}

export function remainingTime(seconds: number | null | undefined): string | undefined {
	const measured = measuredDownloadTime(seconds);
	return measured ? `${measured} left` : undefined;
}

export function welcomeModelMeta(name: string, size: string, etaSeconds: number | null | undefined, stalled: boolean | undefined): string {
	return [name, size, 'runs in oMLX', stalled ? 'Download paused' : measuredDownloadTime(etaSeconds)]
		.filter(Boolean)
		.join(' · ');
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

/**
 * What the person checked on Welcome and where each row on the step stands. `waiting` is on its way (installing or
 * signing in), `idle` is back at rest (a cancelled install), `failed` needs Try again, and `keyOpen` is a key row
 * still waiting for its key. Only rows the screen shows are counted.
 */
export type PickedTools = { waiting: string[]; idle: string[]; failed: string[]; localReady: boolean; keyOpen: boolean };

const joined = (names: string[]) => names.join(' or ');
/** "a", "a or b", "a, b or c": the ways the button can light, said once however many there are. */
const routes = (parts: string[]) => parts.length > 1 ? `${parts.slice(0, -1).join(', ')} or ${parts[parts.length - 1]}` : parts[0];

/** The line beside the primary button on the AI step: what the button is waiting for, named from the rows shown. */
export function setupFinishNote(source: ModelSource | undefined, downloading: boolean, startError: string, picked?: PickedTools): string {
	if (startError || source?.setup_error) return 'The local model couldn’t start. Try again, or choose another source.';
	const eta = measuredDownloadTime(source?.download_eta_seconds);
	const progressing = downloading && !source?.download_stalled;
	// The download lights the button as much as a tool does, so it is said the same way wherever it is a route.
	const download = progressing ? `the download finishes${eta ? ` in ${eta}` : ''}` : null;
	if (picked?.failed.length && picked.localReady) return 'Chat runs on the local model for now.';
	if (picked?.waiting.length) return `Ready when ${routes([`${joined(picked.waiting)} is signed in`, ...(picked.keyOpen ? ['you save your key'] : []), ...(download ? [download] : [])])}.`;
	if (picked?.keyOpen) {
		if (download) return `Ready when ${routes(['you save your key', download])}.`;
		return downloading ? 'Ready when you save your key. The download is paused.' : 'Save your API key to continue.';
	}
	if (!downloading) {
		if (picked?.failed.length) return `Choose Try again on ${joined(picked.failed)} to continue.`;
		return picked?.idle.length ? `Set up ${joined(picked.idle)} to continue.` : 'Set up Claude Code or Codex to continue.';
	}
	if (source?.download_stalled) return 'Download paused. It will resume when progress returns.';
	return `Ready when ${download}. You can close this window; CORSIA keeps going.`;
}

/** The This computer row at rest: what a local model is, and its download size only when it is known. */
export const localRowMeta = (modelSize: string) => `Free and private, and works offline.${modelSize ? ` ${modelSize} download.` : ''}`;

export function setupSourceMeta(source: ModelSource, startError: string, modelSize: string): string {
	if (source.source === 'omlx') {
		if (source.setup_downloading || source.setup_stage) {
			if (source.download_stalled) return 'Download paused';
			const eta = remainingTime(source.download_eta_seconds);
			return source.download_percent == null && source.setup_stage ? source.setup_stage : ['Downloading in the background', eta].filter(Boolean).join(' · ');
		}
		if (source.setup_error || startError) return 'Download needs attention';
		return source.omlx_management?.cannot_check ? cannotCheck(source.omlx_management.required_version) : localRowMeta(modelSize);
	}
	if (source.state === 'not_installed') return `${source.name} needs its official app installed first.`;
	if (source.state === 'needs_auth') return 'Sign in to use your subscription';
	return sourceIsReady(source) ? 'Ready to use.' : 'Check this source, then try again.';
}
