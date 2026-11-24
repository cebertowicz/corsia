/** The soft keyboard is up when the visual viewport (`--doc-visual-viewport-height`, kept by the page) is well short of the layout viewport. No keyboard is shorter than this. */
const KEYBOARD_MIN = 120;
function keyboardUp() {
	const seen = parseFloat(document.documentElement.style.getPropertyValue('--doc-visual-viewport-height'));
	return Number.isFinite(seen) && window.innerHeight - seen >= KEYBOARD_MIN;
}

/**
 * Keep the footer fixed unless it takes room a reader needs. A thread's footer scrolls when its fixed rows leave less than one
 * reply target. The list's footer, and its Accept all, scroll while the keyboard is up, so the list keeps that room.
 */
export function documentThreadFooterRoom(drawer: HTMLElement, onChange: (scrolls: boolean) => void) {
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	function measure() {
		const list = drawer.querySelector<HTMLElement>(':scope > .drawer-comments');
		const footer = drawer.querySelector<HTMLElement>('.drawer-foot');
		const back = drawer.querySelector<HTMLElement>(':scope > .thread-back');
		if (!list || !footer) { onChange(false); return; }
		if (!back) { onChange(keyboardUp()); return; }
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
	// The page publishes the visual viewport on its own resize handler; measuring a frame later reads what it set.
	window.visualViewport?.addEventListener('resize', schedule);
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
	return { destroy() { window.visualViewport?.removeEventListener('resize', schedule); cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); onChange(false); } };
}
