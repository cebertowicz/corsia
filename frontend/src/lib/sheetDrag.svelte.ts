import { releaseSheetDrag } from '$lib/sheetMotion';

/**
 * A drag on a sheet's grabber: how far the sheet follows the pointer down, and what letting go does (`releaseSheetDrag`).
 * One pointer at a time; a press with any button but the primary one is ignored.
 */
export function createSheetDrag(props: {
	detent: () => 'full' | 'half';
	/** The sheet's height when the drag ends, for its dismiss distance. */
	height: () => number;
	onExpand: () => void;
	onDismiss: () => void;
}) {
	let dragY = $state(0);
	let dragging = $state(false);
	let dragPointer: number | null = null;
	let dragStartY = 0;
	let dragStartedAt = 0;
	let dragRaw = 0;

	function startDrag(event: PointerEvent): void {
		if (event.button !== 0) return;
		const handle = event.currentTarget as HTMLElement;
		dragPointer = event.pointerId;
		dragStartY = event.clientY;
		dragStartedAt = performance.now();
		dragY = 0;
		dragRaw = 0;
		dragging = true;
		handle.setPointerCapture?.(event.pointerId);
	}

	function moveDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		dragRaw = event.clientY - dragStartY;
		dragY = Math.max(0, dragRaw);
	}

	function finishDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		const release = releaseSheetDrag({ detent: props.detent(), released: event.type === 'pointerup', raw: dragRaw, elapsed: performance.now() - dragStartedAt, height: props.height() });
		dragging = false;
		dragPointer = null;
		if (release === 'expand') props.onExpand();
		else if (release === 'dismiss') props.onDismiss();
		else dragY = 0;
	}

	return {
		/** How far the sheet sits below its resting place, in px. */
		get y() { return dragY; },
		get dragging() { return dragging; },
		start: startDrag,
		move: moveDrag,
		finish: finishDrag,
	};
}
