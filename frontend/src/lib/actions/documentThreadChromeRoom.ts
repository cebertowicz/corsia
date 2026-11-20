/**
 * Room a thread's reply composer needs in the one scroller: a two-line reply field (65px), the 4px gap and the 24px
 * "Goes to" line (93px), rounded up to 96px. Send sits beside the field, inside that height.
 */
export const COMPOSER_ROOM = 96;

/**
 * Put a thread's Back on the heading's row when the sheet leaves the scroller less than COMPOSER_ROOM, as on a phone held
 * sideways under its keyboard (measured at 852x228: 47px of scroller, 48px of it taken by the Back row). Back stays fixed,
 * 44px tall; nothing is hidden. A taller sheet is untouched. The measure is the sheet's own height, so a keyboard that
 * resizes only the visual viewport counts too. `--back-room` is Back's width, which the heading's title clears.
 */
export function documentThreadChromeRoom(drawer: HTMLElement) {
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	function clear() {
		drawer.removeAttribute('data-short-chrome');
		drawer.style.removeProperty('--back-room');
	}
	function measure() {
		const list = drawer.querySelector<HTMLElement>(':scope > .drawer-comments');
		const back = drawer.querySelector<HTMLElement>(':scope > .thread-back');
		if (!list || !back) { clear(); return; }
		const short = drawer.hasAttribute('data-short-chrome');
		const gap = parseFloat(getComputedStyle(drawer).rowGap) || 0;
		// Once Back is out of the flow its row is gone: count it back in, as the normal layout would have it.
		const room = list.clientHeight - (short ? back.offsetHeight + gap : 0);
		if (room >= COMPOSER_ROOM) { clear(); return; }
		drawer.setAttribute('data-short-chrome', '');
		const button = back.querySelector<HTMLElement>('.drawer-back');
		drawer.style.setProperty('--back-room', `${button?.offsetWidth ?? 0}px`);
	}
	function schedule() {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(measure);
	}
	function observe() {
		resize.disconnect();
		resize.observe(drawer);
		for (const row of drawer.children) resize.observe(row);
		schedule();
	}
	const mutation = new MutationObserver(observe);
	mutation.observe(drawer, { childList: true });
	observe();
	return { destroy() { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); clear(); } };
}
