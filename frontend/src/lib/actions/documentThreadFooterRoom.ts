/** Keep the footer fixed unless a thread's fixed rows leave less than one reply target. */
export function documentThreadFooterRoom(drawer: HTMLElement, onChange: (scrolls: boolean) => void) {
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	function measure() {
		const list = drawer.querySelector<HTMLElement>(':scope > .drawer-comments');
		const footer = drawer.querySelector<HTMLElement>('.drawer-foot');
		const back = drawer.querySelector<HTMLElement>(':scope > .thread-back');
		if (!list || !footer || !back) { onChange(false); return; }
		// Back is out of the flow on a short sheet (documentThreadChromeRoom), so it takes no row.
		const fixed = [...drawer.children].filter((child) => child !== list && child !== footer && getComputedStyle(child).position !== 'absolute');
		const gap = parseFloat(getComputedStyle(drawer).rowGap) || 0;
		const required = fixed.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0)
			+ footer.getBoundingClientRect().height + gap * (fixed.length + 1) + 44;
		onChange(required > drawer.clientHeight);
	}
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
	return { destroy() { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); onChange(false); } };
}
