/** Width of the margin rail, and the side of one marker's hit box, for a fine pointer. */
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

export interface MarkerLine {
	/** Top of the line's first visual row, in document coordinates. */
	top: number;
	ids: string[];
}

export interface MarkerCluster {
	/** The anchor: the first row of the cluster. */
	top: number;
	ids: string[];
}

/**
 * Groups annotated lines, given in document order, into markers. A line joins the current cluster only while its
 * first row lies less than one target height below the cluster's anchor, the cluster's first row and never the last
 * row added, so a run of lines cannot chain a cluster across unrelated paragraphs. Each id appears once.
 */
export function clusterMarkerLines(lines: MarkerLine[], targetHeight: number): MarkerCluster[] {
	const clusters: MarkerCluster[] = [];
	const seen = new Set<string>();
	for (const line of lines) {
		const ids = [...new Set(line.ids)].filter((id) => !seen.has(id));
		if (!ids.length) continue;
		ids.forEach((id) => seen.add(id));
		const current = clusters.at(-1);
		if (current && line.top - current.top < targetHeight) current.ids.push(...ids);
		else clusters.push({ top: line.top, ids });
	}
	return clusters;
}
