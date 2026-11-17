/** How long after a resize CodeMirror measures it (its resize observer's delay); its anchor scroll for the rewrap comes then. */
const EDITOR_RESIZE_MEASURE_MS = 50;

/** A watch on an editor's width while its text rewraps at a new one; see `watchWidthSettle`. */
export interface WidthSettle {
	/** Call from each resize observation that sees a width other than the settled one. */
	changed(): void;
	/** Stops watching. */
	stop(): void;
}

/**
 * Watches the scroller's width, once it changes, until the rewrap is laid: the first frame after CodeMirror's own resize
 * measure in which the width is the one seen in the frame before and `lay` (which lays the reader's place and says
 * whether that moved it) moves nothing. `settled` then gets that width. The watch ends early once `alive` is false.
 */
export function watchWidthSettle(
	scroller: HTMLElement,
	alive: () => boolean,
	lay: () => boolean,
	settled: (width: number) => void,
): WidthSettle {
	// The width last seen while it changes, whether it has been resized since the last frame, when (in frame time) the
	// watch first saw the change, whether a frame has begun since CodeMirror's resize measure was due (so that measure
	// has run), and the frame asked to see whether the width has settled.
	let laidWidth = scroller.clientWidth;
	let resized = false;
	let changedAt = 0;
	let measured = false;
	let settling = 0;
	const next = () => {
		settling = requestAnimationFrame((now) => {
			settling = 0;
			if (!alive()) return;
			const width = scroller.clientWidth;
			if (resized || width !== laidWidth) [laidWidth, resized, changedAt, measured] = [width, false, now, false];
			else if (!lay() && measured) {
				settled(width);
				return;
			} else measured = now - changedAt >= EDITOR_RESIZE_MEASURE_MS;
			next();
		});
	};
	return {
		changed() {
			if (scroller.clientWidth !== laidWidth) resized = true;
			if (!settling) next();
		},
		stop() {
			if (settling) cancelAnimationFrame(settling);
		},
	};
}
