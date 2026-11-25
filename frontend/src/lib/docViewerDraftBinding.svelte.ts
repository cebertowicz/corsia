import type { DocViewerTarget } from '$lib/docViewerTarget';
import { flushDraft, setDraftText } from '$lib/stores/docSaveQueue';

export function bindViewerDraft(source: { target: () => DocViewerTarget | null; text: () => string; canEdit: () => boolean; canFlush: () => boolean }) {
	$effect(() => {
		const text = source.text();
		const target = source.target();
		if (!target || !source.canEdit()) return;
		setDraftText(target, text);
	});
	/** Saves every pending edit now. Resolves true once the server holds them (or there is nothing to save), false on a failed save. */
	return function flushNow(): Promise<boolean> {
		const target = source.target();
		return target && source.canFlush() ? flushDraft(target) : Promise.resolve(true);
	};
}
