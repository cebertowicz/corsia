import { EditorView } from '@codemirror/view';

/** The input that actually closed this surface, rather than the device's capabilities. */
export function isTouchClose(event: { type: string; pointerType?: string; detail?: number } | undefined): boolean {
	return (event?.type === 'pointerdown' || event?.type === 'pointerup' || event?.type === 'click') && (event.pointerType === 'touch' || event.pointerType === 'pen');
}

/** One surface owns its pending pointer, consumed by the corresponding activation. */
export function createCloseInput() {
	let pointer: { target: EventTarget | null; touch: boolean } | null = null;
	let touch = false;
	return {
		get touch() { return touch; },
		pointerDown(event: PointerEvent) { pointer = { target: event.target, touch: isTouchClose(event) }; },
		close(event?: Event): boolean {
			const preceding = pointer;
			pointer = null;
			if (!event) return touch = false;
			if (event instanceof MouseEvent && event.type === 'click' && event.detail > 0 && preceding?.target === event.target) return touch = preceding.touch;
			return touch = event instanceof PointerEvent && !!event.pointerType && isTouchClose(event);
		},
		clear() { pointer = null; touch = false; },
	};
}

/** Focus can move any scrolling ancestor even when the browser ignores preventScroll. */
export function focusWithoutMoving(target: HTMLElement, focus: () => void): void {
	const offsets: { element: HTMLElement; top: number; left: number }[] = [];
	for (let element: HTMLElement | null = target; element; element = element.parentElement) {
		offsets.push({ element, top: element.scrollTop, left: element.scrollLeft });
	}
	const x = window.scrollX, y = window.scrollY;
	try { focus(); } finally {
		for (const { element, top, left } of offsets) { element.scrollTop = top; element.scrollLeft = left; }
		if (window.scrollX !== x || window.scrollY !== y) window.scrollTo(x, y);
	}
}

/** Restore CodeMirror's selection before focusing its editable DOM. */
export function returnFocus(target: HTMLElement | null | undefined): void {
	if (!target) return;
	focusWithoutMoving(target, () => {
		const view = target.closest('.cm-editor') && EditorView.findFromDOM(target);
		if (!view || target !== view.contentDOM) { target.focus({ preventScroll: true }); return; }
		const { anchor, head } = view.state.selection.main;
		const a = view.domAtPos(anchor), h = view.domAtPos(head);
		document.getSelection()?.setBaseAndExtent(a.node, a.offset, h.node, h.offset);
		view.focus();
	});
}
