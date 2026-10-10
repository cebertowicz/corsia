export type DragKind = 'sidebar' | 'pane';

export function getSlideWidth(innerWidth: number): number {
	return Math.min(320, innerWidth * 0.85);
}

export function getPaneWidth(innerWidth: number): number {
	return Math.min(420, innerWidth * 0.92);
}

export const OVER_OPEN_RUBBER = 0.15;

export function trackDrag(kind: DragKind, raw: number, slideWidth: number, paneWidth: number): number {
	if (kind === 'pane') {
		if (raw > 0) return 0;
		if (raw < -paneWidth) return -paneWidth + (raw + paneWidth) * OVER_OPEN_RUBBER;
		return raw;
	}
	if (raw < 0) return 0;
	if (raw > slideWidth) return slideWidth + (raw - slideWidth) * OVER_OPEN_RUBBER;
	return raw;
}

export const FLICK_VELOCITY = 0.5;
export const VELOCITY_STALE_MS = 100;

export function releaseVelocity(
	prevX: number,
	prevT: number,
	lastX: number,
	lastT: number,
	releaseT: number,
): number {
	const dt = lastT - prevT;
	if (dt <= 0) return 0;
	if (releaseT - lastT > VELOCITY_STALE_MS) return 0;
	return (lastX - prevX) / dt;
}

export function shouldOpenOnRelease(
	kind: DragKind,
	dx: number,
	slideWidth: number,
	paneWidth: number,
	velocityX = 0,
): boolean {
	if (Math.abs(velocityX) >= FLICK_VELOCITY) {
		return kind === 'pane' ? velocityX < 0 : velocityX > 0;
	}
	return kind === 'pane'
		? dx < -paneWidth * 0.4
		: dx > slideWidth * 0.4;
}
