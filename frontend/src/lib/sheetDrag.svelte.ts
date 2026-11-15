import { releaseSheetDrag } from '$lib/sheetMotion';
import { holdSheet } from '$lib/sheetClearance';

/** How long a let-go sheet takes to settle: its transform and max-height transitions run 220 ms (AdaptiveDrawer), plus a frame or two. */
const SETTLE_MS = 300;

/**
 * A drag on a sheet's grabber: how far the sheet follows the pointer down, how far up it is pulled while it can grow,
 * and what letting go does (`releaseSheetDrag`). One pointer at a time; a press with any button but the primary one is ignored.
 * From the press until the sheet settles the sheet is held (`holdSheet`), so nothing moves the page or re-caps the sheet under the finger.
 */
export function createSheetDrag(props: {
	/** The sheet is shorter than it may be, so pulling it up grows it. Read once as the drag starts. */
	expandable: () => boolean;
	/** The sheet's panel: held while dragged, and its height when the drag ends sets the dismiss distance. */
	panel: () => HTMLElement | null;
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
	let held: HTMLElement | null = null;
	let settling: ReturnType<typeof setTimeout> | undefined;

	function hold(panel: HTMLElement | null): void {
		clearTimeout(settling);
		if (held && held !== panel) holdSheet(held, false);
		held = panel;
		if (panel) holdSheet(panel, true);
	}
	function letGo(): void {
		clearTimeout(settling);
		const panel = held;
		if (panel) settling = setTimeout(() => { holdSheet(panel, false); if (held === panel) held = null; }, SETTLE_MS);
	}

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
		hold(props.panel());
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
		const release = releaseSheetDrag({ expandable: canGrow, released: event.type === 'pointerup', raw: dragRaw, elapsed: performance.now() - dragStartedAt, height: props.panel()?.clientHeight ?? 480 });
		dragging = false;
		dragPointer = null;
		pullY = 0;
		letGo();
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
		reset: () => { dragY = 0; pullY = 0; hold(null); },
	};
}
