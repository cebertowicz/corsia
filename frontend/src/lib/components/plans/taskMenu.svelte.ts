import type { OperatorNode } from '$lib/api/plans';
import { createRowMenu } from '$lib/components/ui/rowMenu.svelte';

export interface OpenTaskMenu {
	node: OperatorNode;
	x: number;
	y: number;
	/** What had focus, so closing returns it there. */
	trigger: HTMLElement | null;
}

/**
 * A task row's action menu: the shared row menu (`ui/rowMenu`) over plan
 * nodes. Right-click, Shift+F10 or the context-menu key, and a touch
 * long-press open it on a row the user can act on.
 */
export function createTaskMenu(canAct: (node: OperatorNode) => boolean) {
	const menu = createRowMenu<OperatorNode>(canAct);
	return {
		get open(): OpenTaskMenu | null {
			const open = menu.open;
			return open && { node: open.item, x: open.x, y: open.y, trigger: open.trigger };
		},
		close: menu.close,
		onContextMenu: menu.onContextMenu,
		onKeydown: menu.onKeydown,
		onPointerDown: menu.onPointerDown,
		onPointerMove: menu.onPointerMove,
		onPointerEnd: menu.onPointerEnd,
		onClickCapture: menu.onClickCapture,
		rowHandlers: menu.rowHandlers,
	};
}
