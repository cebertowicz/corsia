/** How long a finger rests on a row before it opens the menu. */
export const LONG_PRESS_MS = 500;
/** A finger that travels further than this is scrolling, not pressing. */
const PRESS_SLOP_PX = 10;

export interface OpenRowMenu<T> {
	item: T;
	x: number;
	y: number;
	/** What had focus, so closing returns it there. */
	trigger: HTMLElement | null;
}

/**
 * Opens a row's `ui/Menu` by right-click, Shift+F10 or the context-menu key on
 * a focused row, and a touch
 * long-press whose ending tap is swallowed so it activates neither the row nor
 * the menu item that opened under the finger. A row the user has nothing to do
 * on leaves the browser's own menu alone.
 */
export function createRowMenu<T>(canAct: (item: T) => boolean) {
	let open = $state<OpenRowMenu<T> | null>(null);
	let pressTimer: ReturnType<typeof setTimeout> | null = null;
	let pressFrom: { x: number; y: number } | null = null;
	let disarmTap: (() => void) | null = null;

	/**
	 * The press that opened the menu still ends in a click wherever the finger lifts,
	 * which is over the menu, since it opens under the finger. Swallow that one click
	 * anywhere on the page, and nothing once the next press starts.
	 */
	function swallowEndingTap() {
		disarmTap?.();
		const swallow = (event: MouseEvent) => {
			event.preventDefault();
			event.stopPropagation();
			disarm();
		};
		const disarm = () => {
			window.removeEventListener('click', swallow, true);
			window.removeEventListener('pointerdown', disarm, true);
			disarmTap = null;
		};
		window.addEventListener('click', swallow, true);
		window.addEventListener('pointerdown', disarm, true);
		disarmTap = disarm;
	}

	function show(item: T, x: number, y: number, trigger: HTMLElement | null) {
		open = { item, x, y, trigger };
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
		/** Opens the menu for a row from code (a link that lands on it), below the row as the menu key does. */
		openFor(item: T, row: HTMLElement) {
			if (!canAct(item)) return;
			const box = row.getBoundingClientRect();
			show(item, box.left, box.bottom, row);
		},
		onContextMenu(event: MouseEvent, item: T) {
			if (!canAct(item)) return;
			event.preventDefault();
			show(item, event.clientX, event.clientY, event.target as HTMLElement | null);
		},
		onKeydown(event: KeyboardEvent, item: T) {
			const asked = event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey);
			if (!asked || !canAct(item)) return;
			event.preventDefault();
			const target = event.target as HTMLElement;
			const box = target.getBoundingClientRect();
			show(item, box.left, box.bottom, target);
		},
		onPointerDown(event: PointerEvent, item: T) {
			if (event.pointerType !== 'touch' || !canAct(item)) return;
			cancelPress();
			pressFrom = { x: event.clientX, y: event.clientY };
			const { x, y } = pressFrom;
			const trigger = event.target as HTMLElement | null;
			pressTimer = setTimeout(() => {
				cancelPress();
				swallowEndingTap();
				show(item, x, y, trigger);
			}, LONG_PRESS_MS);
		},
		onPointerMove(event: PointerEvent) {
			if (!pressFrom) return;
			const moved = Math.hypot(event.clientX - pressFrom.x, event.clientY - pressFrom.y);
			if (moved > PRESS_SLOP_PX) cancelPress();
		},
		onPointerEnd: cancelPress,
		/** The row's event handlers, spread onto the element so the row stays small. */
		rowHandlers: (item: T) => ({
			oncontextmenu: (event: MouseEvent) => api.onContextMenu(event, item),
			onkeydown: (event: KeyboardEvent) => api.onKeydown(event, item),
			onpointerdown: (event: PointerEvent) => api.onPointerDown(event, item),
			onpointermove: api.onPointerMove,
			onpointerup: api.onPointerEnd,
			onpointercancel: api.onPointerEnd,
		}),
	};
	return api;
}
