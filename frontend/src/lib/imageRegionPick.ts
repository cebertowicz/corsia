import { nearestTarget, type Point } from './imageRegions';

/** Track Space for pan mode and clear it when the window loses focus. */
export function trackSpace(setHeld: (held: boolean) => void): () => void {
	const down = (event: KeyboardEvent) => { if (event.code === 'Space') setHeld(true); };
	const up = (event: KeyboardEvent) => { if (event.code === 'Space') setHeld(false); };
	const blur = () => setHeld(false);
	window.addEventListener('keydown', down);
	window.addEventListener('keyup', up);
	window.addEventListener('blur', blur);
	return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.removeEventListener('blur', blur); };
}

/** Resolve the visible targets for a pointer; touch and pen expose every ambiguous candidate. */
export function imageRegionPick(
	targets: Array<Point & { id: string; moving: boolean }>, point: Point, pointerType: string,
): string | string[] | null {
	if (pointerType === 'touch' || pointerType === 'pen') {
		const candidates = targets.filter((target) => !target.moving && Math.abs(target.x - point.x) <= 22 && Math.abs(target.y - point.y) <= 22);
		if (candidates.length > 1) return candidates.map((target) => target.id);
		if (candidates.length === 1) return candidates[0].id;
	}
	return nearestTarget(targets, point);
}
