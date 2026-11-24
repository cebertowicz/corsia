import { coarsePointer } from '$lib/selectionButtonPlace';
import { isEditableTarget } from '$lib/editableTarget';

/**
 * The soft keyboard is up while a text field in the drawer has focus on a touch screen: the page's own keyboard handling
 * (`homeLifecycle`) uses the same test. It reads no viewport size, so it holds whether the browser resizes the layout for the keyboard or not.
 */
function keyboardUp(drawer: HTMLElement) {
	const focused = document.activeElement;
	return coarsePointer() && !!focused && drawer.contains(focused) && isEditableTarget(focused);
}

/**
 * Keep the footer fixed unless it takes room a reader needs. A thread's footer scrolls when its fixed rows leave less than one
 * reply target. The list's footer scrolls while the keyboard is up, so the list keeps that room.
 */
export function documentFooterRoom(drawer: HTMLElement, onChange: (scrolls: boolean) => void) {
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	function measure() {
		const list = drawer.querySelector<HTMLElement>(':scope > .drawer-comments');
		const footer = drawer.querySelector<HTMLElement>('.drawer-foot');
		const back = drawer.querySelector<HTMLElement>(':scope > .thread-back');
		if (!list || !footer) { onChange(false); return; }
		if (!back) { onChange(keyboardUp(drawer)); return; }
		// Back is out of the flow on a short sheet (documentThreadChromeRoom), so it takes no row.
		const fixed = [...drawer.children].filter((child) => child !== list && child !== footer && getComputedStyle(child).position !== 'absolute');
		const style = getComputedStyle(drawer);
		const gap = parseFloat(style.rowGap) || 0;
		// One reply target is the design token's size, read where it applies, so a changed token moves this threshold with it.
		const target = parseFloat(style.getPropertyValue('--touch-target')) || 0;
		const required = fixed.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0)
			+ footer.getBoundingClientRect().height + gap * (fixed.length + 1) + target;
		onChange(required > drawer.clientHeight);
	}
	// Focus moves first and the element it lands on is read a frame later, so leaving one field for another never reads as the keyboard going down.
	drawer.addEventListener('focusin', schedule);
	drawer.addEventListener('focusout', schedule);
	function schedule() {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(measure);
	}
	function observeRows() {
		resize.disconnect();
		resize.observe(drawer);
		for (const row of drawer.children) resize.observe(row);
		const footer = drawer.querySelector<HTMLElement>('.drawer-foot');
		if (footer && footer.parentElement !== drawer) resize.observe(footer);
		schedule();
	}
	const mutation = new MutationObserver((records) => {
		if (records.some((record) => record.target === drawer || (record.target instanceof Element && record.target.matches('.drawer-comments')))) observeRows();
	});
	mutation.observe(drawer, { childList: true, subtree: true });
	observeRows();
	return { destroy() { drawer.removeEventListener('focusin', schedule); drawer.removeEventListener('focusout', schedule); cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); onChange(false); } };
}
