import type { OperatorNode } from '$lib/api/plans';

/** How long a finger rests on a row before it opens the menu. */
export const LONG_PRESS_MS = 500;
/** A finger that travels further than this is scrolling, not pressing. */
const PRESS_SLOP_PX = 10;

export interface OpenTaskMenu {
	node: OperatorNode;
	x: number;
	y: number;
	/** What had focus, so closing returns it there. */
	trigger: HTMLElement | null;
}

/**
 * Opens a task row's action menu three ways: right-click, Shift+F10 or the
 * context-menu key on a focused row, and a touch long-press whose following tap
 * is swallowed so it does not also expand the row. A row the user has nothing to
 * do on leaves the browser's own menu alone.
 */
export function createTaskMenu(canAct: (node: OperatorNode) => boolean) {
	let open = $state<OpenTaskMenu | null>(null);
	let pressTimer: ReturnType<typeof setTimeout> | null = null;
	let pressFrom: { x: number; y: number } | null = null;
	let swallowTap = false;

	function show(node: OperatorNode, x: number, y: number, trigger: HTMLElement | null) {
		open = { node, x, y, trigger };
	}

	function cancelPress() {
		if (pressTimer) clearTimeout(pressTimer);
		pressTimer = null;
		pressFrom = null;
	}

	function close() {
		const trigger = open?.trigger;
		open = null;
		if (trigger?.isConnected) trigger.focus();
	}

	const api = {
		get open() {
			return open;
		},
		close,
		onContextMenu(event: MouseEvent, node: OperatorNode) {
			if (!canAct(node)) return;
			event.preventDefault();
			show(node, event.clientX, event.clientY, event.target as HTMLElement | null);
		},
		onKeydown(event: KeyboardEvent, node: OperatorNode) {
			const asked = event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey);
			if (!asked || !canAct(node)) return;
			event.preventDefault();
			const target = event.target as HTMLElement;
			const box = target.getBoundingClientRect();
			show(node, box.left, box.bottom, target);
		},
		onPointerDown(event: PointerEvent, node: OperatorNode) {
			swallowTap = false;
			if (event.pointerType !== 'touch' || !canAct(node)) return;
			cancelPress();
			pressFrom = { x: event.clientX, y: event.clientY };
			const { x, y } = pressFrom;
			const trigger = event.target as HTMLElement | null;
			pressTimer = setTimeout(() => {
				cancelPress();
				swallowTap = true;
				show(node, x, y, trigger);
			}, LONG_PRESS_MS);
		},
		onPointerMove(event: PointerEvent) {
			if (!pressFrom) return;
			const moved = Math.hypot(event.clientX - pressFrom.x, event.clientY - pressFrom.y);
			if (moved > PRESS_SLOP_PX) cancelPress();
		},
		onPointerEnd: cancelPress,
		/** Capture-phase: the tap that ends a long-press must not expand the row. */
		onClickCapture(event: MouseEvent) {
			if (!swallowTap) return;
			swallowTap = false;
			event.preventDefault();
			event.stopPropagation();
		},
		/** The row's event handlers, spread onto the element so the tree stays small. */
		rowHandlers: (node: OperatorNode) => ({
			oncontextmenu: (event: MouseEvent) => api.onContextMenu(event, node),
			onkeydown: (event: KeyboardEvent) => api.onKeydown(event, node),
			onpointerdown: (event: PointerEvent) => api.onPointerDown(event, node),
			onpointermove: api.onPointerMove,
			onpointerup: api.onPointerEnd,
			onpointercancel: api.onPointerEnd,
			onclickcapture: api.onClickCapture,
		}),
	};
	return api;
}
