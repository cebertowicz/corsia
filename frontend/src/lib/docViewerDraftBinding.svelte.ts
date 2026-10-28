import type { DocViewerTarget } from '$lib/docViewerTarget';
import { flushDraft, setDraftText } from '$lib/stores/docSaveQueue';

export function bindViewerDraft(source: { target: () => DocViewerTarget | null; text: () => string; canEdit: () => boolean; canFlush: () => boolean }) {
	$effect(() => {
		const text = source.text();
		const target = source.target();
		if (!target || !source.canEdit()) return;
		setDraftText(target, text);
	});
	return function flushNow() {
		const target = source.target();
		if (target && source.canFlush()) void flushDraft(target);
	};
}
