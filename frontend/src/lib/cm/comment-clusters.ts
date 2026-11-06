/** Width of the text's left inset for a fine pointer. */
export const FINE_RAIL = 24;
/** The same for a coarse (touch) pointer, or when the pointer cannot be told. */
export const COARSE_RAIL = 44;

export interface PointerEnvironment {
	/** `(any-pointer: coarse)`; undefined when `matchMedia` is unavailable. */
	coarse?: boolean;
	/** `navigator.maxTouchPoints`; undefined when the browser does not report it. */
	touchPoints?: number;
	/** `'ontouchstart' in window`. */
	touchEvents?: boolean;
}

/** Coarse pointer, touch present, or nothing known at all means the larger rail. */
export function markerRailWidth(environment: PointerEnvironment): number {
	if (environment.coarse === true || (environment.touchPoints ?? 0) > 0 || environment.touchEvents === true) return COARSE_RAIL;
	if (environment.coarse === undefined && environment.touchPoints === undefined) return COARSE_RAIL;
	return FINE_RAIL;
}

export function readPointerEnvironment(win: Window = window): PointerEnvironment {
	return {
		coarse: typeof win.matchMedia === 'function' ? win.matchMedia('(any-pointer: coarse)').matches : undefined,
		touchPoints: typeof win.navigator?.maxTouchPoints === 'number' ? win.navigator.maxTouchPoints : undefined,
		touchEvents: 'ontouchstart' in win,
	};
}

/** The rail width this window's pointer gets: the one decision the viewer and the editor both read. */
export function railWidth(win: Window = window): number {
	return markerRailWidth(readPointerEnvironment(win));
}
