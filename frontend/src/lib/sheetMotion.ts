import { cubicOut } from 'svelte/easing';

/** The sheet's slide: up from 72 px below over 260 ms, or none under reduced motion, where the backdrop's fade carries it. */
export function sheetFly(reduced: boolean) {
	return reduced ? { y: 0, duration: 0 } : { y: 72, duration: 260, easing: cubicOut };
}

/** How far a downward drag must go to dismiss: about a quarter of the sheet, held between 96 and 180 px. */
export function dismissDistance(height: number): number {
	return Math.min(180, Math.max(96, height * 0.24));
}

export type SheetRelease = 'expand' | 'dismiss' | 'settle';

/**
 * Where a released grabber drag leaves the sheet. A half sheet goes full on a pointer-up that rose 40 px, or on a
 * quick tap (under 6 px within 350 ms). Any sheet dismisses past its dismiss distance, or past 64 px on a flick faster
 * than 0.75 px/ms. Anything else settles back. A cancelled pointer never expands the sheet.
 */
export function releaseSheetDrag({ detent, released, raw, elapsed, height }: {
	detent: 'full' | 'half';
	/** The pointer was lifted, rather than cancelled. */
	released: boolean;
	/** Vertical travel since the press, negative upwards. */
	raw: number;
	elapsed: number;
	height: number;
}): SheetRelease {
	const time = Math.max(1, elapsed);
	const down = Math.max(0, raw);
	if (detent === 'half' && released && (raw <= -40 || (Math.abs(raw) < 6 && time < 350))) return 'expand';
	if (down >= dismissDistance(height) || (down >= 64 && down / time > 0.75)) return 'dismiss';
	return 'settle';
}
