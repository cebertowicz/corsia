import type { View } from '$lib/homePaneConfig';
import { measurementCloseOrigin } from '$lib/stores/measurementNavigation';

export { measurementViewRequested } from '$lib/stores/measurementAddress';

type ResetSurface = (
	view: View,
	options?: { preserveActiveThread?: boolean },
) => void;

type CloseMeasurementOptions = {
	resetSurface: ResetSurface;
	setActiveThread: (threadId: string) => void;
};

/** Return to the recorded thread doorway, or fall back to Activity. */
export function closeMeasurementSurface({ resetSurface, setActiveThread }: CloseMeasurementOptions): void {
	const origin = measurementCloseOrigin();
	if (origin?.kind !== 'thread') {
		resetSurface('activity');
		return;
	}
	setActiveThread(origin.threadId);
	resetSurface('chat', { preserveActiveThread: true });
	// The former Close target disappears with the page; make the next keyboard
	// target a visible, useful header action after the chat surface commits.
	setTimeout(() => {
		const header = document.querySelector('.shell-header');
		const target = [...(header?.querySelectorAll<HTMLButtonElement>('.h-act[title="Open measures for this lane"], [aria-label="More actions"]') ?? [])]
			.find((button) => button.offsetParent !== null);
		target?.focus();
	}, 100);
}
