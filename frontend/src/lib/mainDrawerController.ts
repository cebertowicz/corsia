import {
	getSlideWidth as calcSlideWidth,
	getPaneWidth as calcPaneWidth,
	trackDrag,
	shouldOpenOnRelease,
	releaseVelocity,
	type DragKind,
} from '$lib/drawerDrag';

interface DrawerControllerOptions {
	getMainEl: () => HTMLElement | undefined;
	getSidebarOpen: () => boolean;
	setSidebarOpen: (open: boolean) => void;
	getPaneOpen: () => boolean;
	setPaneOpen: (open: boolean) => void;
	setPaneDragging: (dragging: boolean) => void;
}

export function createMainDrawerController(options: DrawerControllerOptions) {
	let dragActive = false;
	let dragCommitted = false;
	let dragStartX = 0;
	let dragStartY = 0;
	let dragBaseDx = 0;
	let dragCurrentDx = 0;
	let dragKind: DragKind | null = null;
	let dragPrevX = 0;
	let dragPrevT = 0;
	let dragLastX = 0;
	let dragLastT = 0;
	let drawerAnimTimer: ReturnType<typeof setTimeout> | null = null;
	let releaseTargetListeners: (() => void) | null = null;

	const getSlideWidth = () => typeof window === 'undefined' ? 320 : calcSlideWidth(window.innerWidth);
	const getPaneWidth = () => typeof window === 'undefined' ? 420 : calcPaneWidth(window.innerWidth);
	const applyDragTransform = (dx: number) => {
		const mainEl = options.getMainEl();
		if (!mainEl) return;
		mainEl.style.transition = 'none';
		mainEl.style.transform = `translateX(${dx}px)`;
	};
	const clearDragTransform = () => {
		const mainEl = options.getMainEl();
		if (!mainEl) return;
		mainEl.style.transition = '';
		mainEl.style.transform = '';
	};

	const onTouchStart = (e: TouchEvent) => {
		if (typeof window === 'undefined') return;
		const w = window.innerWidth;
		if (w > 1024) return;
		const t = e.touches[0];
		if (options.getSidebarOpen()) {
			dragKind = 'sidebar';
			dragBaseDx = getSlideWidth();
		} else if (options.getPaneOpen()) {
			dragKind = 'pane';
			dragBaseDx = -getPaneWidth();
		} else if (t.clientX <= 24 && w <= 768) {
			dragKind = 'sidebar';
			dragBaseDx = 0;
		} else if (t.clientX >= w - 24) {
			dragKind = 'pane';
			dragBaseDx = 0;
		} else {
			return;
		}
		// touchend/touchcancel go to the original target even once it is
		// detached (live content re-rendering mid-swipe) and never reach
		// `<main>`, so listen on the target itself to guarantee the drag ends.
		releaseTargetListeners?.();
		const target = e.target;
		if (target instanceof EventTarget && target !== e.currentTarget) {
			const end = (ev: Event) => onTouchEnd(ev as TouchEvent);
			target.addEventListener('touchend', end);
			target.addEventListener('touchcancel', end);
			releaseTargetListeners = () => {
				target.removeEventListener('touchend', end);
				target.removeEventListener('touchcancel', end);
				releaseTargetListeners = null;
			};
		}
		dragStartX = t.clientX;
		dragStartY = t.clientY;
		dragActive = true;
		dragCommitted = false;
		dragCurrentDx = dragBaseDx;
		dragPrevX = dragLastX = t.clientX;
		dragPrevT = dragLastT = e.timeStamp;
	};

	const onTouchMove = (e: TouchEvent) => {
		if (!dragActive) return;
		const t = e.touches[0];
		const dx = t.clientX - dragStartX;
		const dy = t.clientY - dragStartY;
		if (!dragCommitted && Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 8) {
			dragActive = false;
			return;
		}
		if (!dragCommitted && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy) * 1.4) {
			dragCommitted = true;
		}
		if (!dragCommitted) return;
		e.preventDefault();
		if (dragKind === 'pane') options.setPaneDragging(true);
		const raw = dragBaseDx + dx;
		dragCurrentDx = trackDrag(dragKind === 'pane' ? 'pane' : 'sidebar', raw, getSlideWidth(), getPaneWidth());
		applyDragTransform(dragCurrentDx);
		dragPrevX = dragLastX; dragPrevT = dragLastT;
		dragLastX = t.clientX; dragLastT = e.timeStamp;
	};

	const onTouchEnd = (e: TouchEvent) => {
		releaseTargetListeners?.();
		if (!dragActive) return;
		dragActive = false;
		if (!dragCommitted) {
			dragKind = null;
			options.setPaneDragging(false);
			return;
		}
		clearDragTransform();
		const velocityX = releaseVelocity(dragPrevX, dragPrevT, dragLastX, dragLastT, e.timeStamp);
		const open = shouldOpenOnRelease(dragKind === 'pane' ? 'pane' : 'sidebar', dragCurrentDx, getSlideWidth(), getPaneWidth(), velocityX);
		if (dragKind === 'pane') options.setPaneOpen(open);
		else options.setSidebarOpen(open);
		dragKind = null;
		options.setPaneDragging(false);
	};

	// transitionstart/transitionend bubble, so without the target check below
	// a transform transition on any descendant of `.main` (e.g. the composer's
	// plus-icon or a sheet-trigger chevron rotating shut) would arm this same
	// gate — silently swallowing an unrelated ResizeObserver-driven height
	// update while the gate believed the drawer itself was sliding (#1321).
	const onTransitionStart = (e: TransitionEvent) => {
		if (e.propertyName !== 'transform' || e.target !== e.currentTarget) return;
		if (typeof document === 'undefined') return;
		document.documentElement.dataset.drawerAnimating = '1';
		if (drawerAnimTimer) clearTimeout(drawerAnimTimer);
		drawerAnimTimer = setTimeout(() => {
			delete document.documentElement.dataset.drawerAnimating;
		}, 600);
	};

	const onTransitionEnd = (e: TransitionEvent) => {
		if (e.propertyName !== 'transform' || e.target !== e.currentTarget) return;
		if (typeof document === 'undefined') return;
		if (drawerAnimTimer) {
			clearTimeout(drawerAnimTimer);
			drawerAnimTimer = null;
		}
		delete document.documentElement.dataset.drawerAnimating;
	};

	// Safety net for state changed outside a drag (hamburger, backdrop): a
	// stale inline transform would otherwise beat the `.main.open` class.
	const reset = () => {
		releaseTargetListeners?.();
		dragActive = false;
		dragKind = null;
		options.setPaneDragging(false);
		clearDragTransform();
	};

	const attachTouchMove = (el: HTMLElement) => {
		el.addEventListener('touchmove', onTouchMove, { passive: false });
		return () => el.removeEventListener('touchmove', onTouchMove);
	};

	return { onTouchStart, onTouchEnd, onTransitionStart, onTransitionEnd, attachTouchMove, reset };
}
