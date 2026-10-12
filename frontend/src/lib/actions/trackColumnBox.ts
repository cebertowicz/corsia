export type ColumnBox = { top: number; right: number; bottom: number; left: number; height: number };

/**
 * Reports where `column` sits in the viewport, so a sheet portaled out of it can stay over exactly that column.
 * Returns the cleanup. `report` is called only when a value changed.
 */
export function trackColumnBox(column: HTMLElement, report: (box: ColumnBox) => void): () => void {
	let frame = 0, running = 0;
	let last: ColumnBox | null = null;
	const read = () => {
		const rect = column.getBoundingClientRect();
		const next = { top: rect.top, right: innerWidth - rect.right, bottom: innerHeight - rect.bottom, left: rect.left, height: rect.height };
		if (!last || (Object.keys(next) as Array<keyof ColumnBox>).some((key) => last![key] !== next[key])) report(last = next);
	};
	const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(read); };
	// A neighbouring pane slides this column as its own layout transition runs, anywhere in the app shell and
	// not only inside the column's container, and a slide that keeps the column's size never trips the resize
	// observer. Follow every layout transition outside the column and the sheet itself, frame by frame while it
	// runs and once more when it ends.
	const layout = (event: TransitionEvent) => event.target instanceof Element && !column.contains(event.target)
		&& !event.target.closest('[data-adaptive-drawer]') && /^(width|height|padding|margin|transform|translate|left|right|top|bottom|inset|flex|grid)/.test(event.propertyName);
	const follow = () => { read(); if (running > 0) frame = requestAnimationFrame(follow); };
	const started = (event: TransitionEvent) => { if (layout(event) && running++ === 0) { cancelAnimationFrame(frame); follow(); } };
	const ended = (event: TransitionEvent) => { if (layout(event)) { running = Math.max(0, running - 1); schedule(); } };
	read();
	const observer = new ResizeObserver(schedule);
	observer.observe(column);
	addEventListener('resize', schedule);
	document.addEventListener('transitionrun', started, true);
	document.addEventListener('transitionend', ended, true);
	document.addEventListener('transitioncancel', ended, true);
	return () => {
		cancelAnimationFrame(frame); observer.disconnect(); removeEventListener('resize', schedule);
		document.removeEventListener('transitionrun', started, true);
		document.removeEventListener('transitionend', ended, true);
		document.removeEventListener('transitioncancel', ended, true);
	};
}
