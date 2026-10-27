/**
 * The number on the update screen counts up to the real progress instead of jumping to it: it
 * closes most of a gap quickly, then finishes at a fixed minimum speed, so any gap closes in
 * under a second. It never passes the target and never goes back.
 */

/** Points per second at the slowest, so the last points of a gap still arrive promptly. */
export const FLOOR_RATE = 20;
/** A gap closes at this many times its own size per second, which is what makes a large gap fast. */
export const GAP_RATE = 4;

/** The shown value one frame later, `dtMs` after the last frame. */
export function countUp(shown: number, target: number, dtMs: number): number {
	if (target <= shown) return shown;
	const rate = Math.max((target - shown) * GAP_RATE, FLOOR_RATE);
	return Math.min(target, shown + (rate * Math.max(0, dtMs)) / 1000);
}
