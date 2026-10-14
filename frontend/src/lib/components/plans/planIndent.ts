/**
 * How far a plan row, and the details under it, step in per level: the one source for every
 * surface of the plan tree (rows, details, history details, time lines, fold history).
 *
 * Each level steps `INDENT_STEP` while the title still has room. Past that, the step shrinks
 * smoothly (the offset approaches a ceiling), so a row's title keeps `MIN_TITLE` at any depth:
 * no level is refused or hidden, it only sits closer to its parent. With no measured width
 * (not laid out yet, or a test) the step is always the full `INDENT_STEP`.
 */

/** Pixels each level steps in while the title has room. */
export const INDENT_STEP = 22;
/** The least width a row's title keeps, at any depth. */
export const MIN_TITLE = 96;
/** The share of the available indent stepped in linearly before the step starts to shrink. */
const KNEE = 2 / 3;

/** The indent, in pixels, of a row `depth` levels in, in a tree `width` pixels wide (0: unmeasured). */
export function indentOffset(depth: number, width: number, reserve: number): number {
	const linear = depth * INDENT_STEP;
	if (depth <= 0 || width <= 0) return Math.max(0, linear);
	const max = Math.max(0, width - reserve - MIN_TITLE);
	const knee = max * KNEE;
	if (linear <= knee) return linear;
	// Leaves the knee at the full step, then closes in on `max`: each level steps a fixed share less than the last.
	const room = max - knee;
	return knee + room * (1 - Math.exp(-(linear - knee) / room));
}

/** What one level steps in at `depth`: the offset it adds over the level above. */
export function indentStep(depth: number, width: number, reserve: number): number {
	return indentOffset(depth, width, reserve) - indentOffset(depth - 1, width, reserve);
}

/** The offset as the CSS length every surface reads from `--indent`. */
export function indentVar(depth: number, width: number, reserve: number): string {
	return `${Math.round(indentOffset(depth, width, reserve) * 100) / 100}px`;
}
