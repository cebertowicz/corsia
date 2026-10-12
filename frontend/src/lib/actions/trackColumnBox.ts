export type ColumnBox = { top: number; right: number; bottom: number; left: number; height: number };

/** A layout transition can only move the column for as long as the longest one runs; this is the longest, in ms. */
const FOLLOW_LIMIT_MS = 420;
/** Frames the box must hold still, once a transition has started, before it is taken to have arrived. */
const STILL_FRAMES = 2;
const LAYOUT_PROPERTY = /^(width|height|padding|margin|left|right|top|bottom|inset|flex|grid)/;

/**
 * Reports where `column` sits in the viewport, so a sheet portaled out of it can stay over exactly that column.
 * Returns the cleanup. `report` is called only when a value changed.
 *
 * Scale budget: nothing runs while the page is still. A layout transition anywhere in the shell starts a
 * follow that reads the column's rect once per frame and ends when the box has held still for two frames or
 * after 420ms, whichever comes first; a further transition restarts it. It is never counted from start and end
 * events, so a node removed mid-transition (whose end never arrives) cannot keep it running.
 */
export function trackColumnBox(column: HTMLElement, report: (box: ColumnBox) => void): () => void {
	let once = 0, loop = 0, followUntil = 0, still = 0;
	let last: ColumnBox | null = null;
	/** Reads the rect; true when it changed. */
	const read = () => {
		const rect = column.getBoundingClientRect();
		const next = { top: rect.top, right: innerWidth - rect.right, bottom: innerHeight - rect.bottom, left: rect.left, height: rect.height };
		const changed = !last || (Object.keys(next) as Array<keyof ColumnBox>).some((key) => last![key] !== next[key]);
		if (changed) report(last = next);
		return changed;
	};
	const schedule = () => { cancelAnimationFrame(once); once = requestAnimationFrame(() => { read(); }); };
	// A neighbouring pane slides this column as its own layout transition runs, anywhere in the app shell and
	// not only inside the column's container, and a slide that keeps the column's size never trips the resize
	// observer. Follow it frame by frame. Only properties that lay out count: a press or hover animating
	// transform never moves the column, and must not start a follow.
	const layout = (event: TransitionEvent) => event.target instanceof Element && !column.contains(event.target)
		&& !event.target.closest('[data-adaptive-drawer]') && LAYOUT_PROPERTY.test(event.propertyName);
	const follow = () => {
		still = read() ? 0 : still + 1;
		loop = still >= STILL_FRAMES || performance.now() > followUntil ? 0 : requestAnimationFrame(follow);
	};
	const started = (event: TransitionEvent) => {
		if (!layout(event)) return;
		followUntil = performance.now() + FOLLOW_LIMIT_MS;
		still = 0;
		if (loop === 0) loop = requestAnimationFrame(follow);
	};
	const ended = (event: TransitionEvent) => { if (layout(event)) schedule(); };
	read();
	const observer = new ResizeObserver(schedule);
	observer.observe(column);
	addEventListener('resize', schedule);
	document.addEventListener('transitionrun', started, true);
	document.addEventListener('transitionend', ended, true);
	document.addEventListener('transitioncancel', ended, true);
	return () => {
		cancelAnimationFrame(once); cancelAnimationFrame(loop); observer.disconnect(); removeEventListener('resize', schedule);
		document.removeEventListener('transitionrun', started, true);
		document.removeEventListener('transitionend', ended, true);
		document.removeEventListener('transitioncancel', ended, true);
	};
}
