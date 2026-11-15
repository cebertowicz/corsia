import { releaseSheetDrag } from '$lib/sheetMotion';

/**
 * A drag on a sheet's grabber: how far the sheet follows the pointer down, how far up it is pulled while it can grow,
 * and what letting go does (`releaseSheetDrag`). One pointer at a time; a press with any button but the primary one is ignored.
 */
export function createSheetDrag(props: {
	/** The sheet is shorter than it may be, so pulling it up grows it. Read once as the drag starts. */
	expandable: () => boolean;
	/** The sheet's height when the drag ends, for its dismiss distance. */
	height: () => number;
	onExpand: () => void;
	onDismiss: () => void;
}) {
	let dragY = $state(0);
	let pullY = $state(0);
	let dragging = $state(false);
	let dragPointer: number | null = null;
	let dragStartY = 0;
	let dragStartedAt = 0;
	let dragRaw = 0;
	let canGrow = false;

	function startDrag(event: PointerEvent): void {
		if (event.button !== 0) return;
		const handle = event.currentTarget as HTMLElement;
		dragPointer = event.pointerId;
		dragStartY = event.clientY;
		dragStartedAt = performance.now();
		dragY = 0;
		pullY = 0;
		dragRaw = 0;
		canGrow = props.expandable();
		dragging = true;
		handle.setPointerCapture?.(event.pointerId);
	}

	function moveDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		dragRaw = event.clientY - dragStartY;
		dragY = Math.max(0, dragRaw);
		pullY = canGrow ? Math.max(0, -dragRaw) : 0;
	}

	function finishDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		const release = releaseSheetDrag({ expandable: canGrow, released: event.type === 'pointerup', raw: dragRaw, elapsed: performance.now() - dragStartedAt, height: props.height() });
		dragging = false;
		dragPointer = null;
		pullY = 0;
		if (release === 'expand') props.onExpand();
		else if (release === 'dismiss') props.onDismiss();
		else dragY = 0;
	}

	return {
		/** How far the sheet sits below its resting place, in px. */
		get y() { return dragY; },
		/** How far the sheet has been pulled up past its height while it can grow, in px; it grows by that much, up to its full height. */
		get pull() { return pullY; },
		get dragging() { return dragging; },
		start: startDrag,
		move: moveDrag,
		finish: finishDrag,
		/** Back to the resting place. A dismissing drag leaves the sheet where it was let go, for its outro. */
		reset: () => { dragY = 0; pullY = 0; },
	};
}
