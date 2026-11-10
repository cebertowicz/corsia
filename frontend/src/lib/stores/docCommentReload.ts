import type { DocComment } from './docComments';

/**
 * A placement reload's flags on a live document's rows. A reload places lost comments again: it runs on its own,
 * once per live load, with nothing shown, so a failure leaves them lost, as they were, and is only recorded.
 */

/** The flags a fresh comments load keeps from the row it replaces, so a reload in flight survives the load. */
export function reloadFlags(old: DocComment | undefined): Pick<DocComment, 'placementReloading' | 'placementReloadError'> {
	return old?.placementReloading || old?.placementReloadError
		? { placementReloading: old.placementReloading, placementReloadError: old.placementReloadError } : {};
}

/** Every anchored row goes back to Placing for the reload. */
export function markReloading(rows: DocComment[]): DocComment[] {
	return rows.map(row => row.anchor && !row.review
		// A row with no placement has never been drawn; keep its orphan eligibility while Reload is in flight.
		? {
			...row,
			placement: row.placement === undefined && !row.drawn ? undefined : 'placing',
			placementReloading: true,
			placementReloadError: false,
		}
		: row);
}

/** The server refused a request: rows still Placing are not placed, and a refused reload marks its rows. */
export function refuseRequest(rows: DocComment[], reload: boolean): DocComment[] {
	return rows.map(row => row.placement === 'placing'
		? { ...row, placement: 'not_placed', placementReloadError: reload ? row.placementReloading : row.placementReloadError }
		: reload && row.placementReloading ? { ...row, placementReloadError: true } : row);
}

/** Records a refused reload once; the person sees nothing new. */
export function reportReloadFailure(artifactId: string, kind: string | undefined): void {
	console.error('Comment placement: placing lost comments again failed', { artifactId, kind });
}

/** A reload that was answered clears its rows' refusal. */
export function clearReloadErrors(rows: DocComment[]): DocComment[] {
	return rows.some(row => row.placementReloading && row.placementReloadError)
		? rows.map(row => row.placementReloading ? { ...row, placementReloadError: false } : row)
		: rows;
}

/** The reload is over, answered or not. */
export function settleReload(rows: DocComment[]): DocComment[] {
	return rows.some(row => row.placementReloading)
		? rows.map(row => row.placementReloading ? { ...row, placementReloading: false } : row)
		: rows;
}
