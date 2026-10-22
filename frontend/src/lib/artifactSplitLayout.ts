// Split derived from actual canvas width, not window width — see
// C0-ARTIFACT-LEAF-SHELL-DESIGN.md "Desktop UX".
export const CHAT_MIN = 420;
export const ARTIFACT_MIN = 360;
// A new desktop canvas begins with chat at 40% and the artifact at 60%.
// The split layout still protects both minimum readable widths below this size.
export const ARTIFACT_DEFAULT_FRACTION = 0.6;
export const DIVIDER_WIDTH = 8;
export const KEYBOARD_STEP = 24;
export const PANE_DRAWER_WIDTH = 312;
export const PANE_DRAWER_MAX_VIEWPORT = 1024;

export function clamp(value: number, min: number, max: number): number {
	if (max < min) return min;
	return Math.min(max, Math.max(min, value));
}

export function computeDefaultArtifactWidth(canvasWidth: number): number {
	return canvasWidth * ARTIFACT_DEFAULT_FRACTION;
}

export const COMBINED_MIN = CHAT_MIN + ARTIFACT_MIN + DIVIDER_WIDTH;

export function isCompactCanvas(canvasWidth: number): boolean {
	return canvasWidth > 0 && canvasWidth < COMBINED_MIN;
}

/**
 * The desktop drawer remains inline only while the canvas can still host both
 * chat and an artifact at their existing combined minimum.
 */
export function shouldOverlayPane(
	hasVisibleArtifactLeaf: boolean,
	viewportWidth: number,
	inlineCanvasWidth: number,
): boolean {
	return hasVisibleArtifactLeaf
		&& viewportWidth > PANE_DRAWER_MAX_VIEWPORT
		&& inlineCanvasWidth > 0
		&& inlineCanvasWidth < COMBINED_MIN;
}

export type SplitLayout =
	| { mode: 'chat-only' }
	| { mode: 'rail' }
	| { mode: 'compact' }
	| { mode: 'split'; artifactWidth: number };

/**
 * Presentation is a pure function of the *canvas* width, never the window's.
 *
 * Below the combined minima there is no room to show chat and an artifact at
 * once, so the canvas presents one surface at a time (compact) rather than
 * squeezing both. The rail only exists on a canvas wide enough to split: on a
 * narrow canvas a collapsed leaf simply means chat is the surface on screen.
 */
export function computeSplitLayout(
	canvasWidth: number,
	hasTabs: boolean,
	leafVisible: boolean,
	preferredArtifactWidth: number | null,
): SplitLayout {
	if (!hasTabs) return { mode: 'chat-only' };
	// A zero width means the canvas has not been measured yet; treat it as wide
	// so the desktop split does not flash through compact on first paint.
	const measured = canvasWidth > 0 ? canvasWidth : COMBINED_MIN;
	if (isCompactCanvas(canvasWidth)) return { mode: 'compact' };
	if (!leafVisible) return { mode: 'rail' };
	const maxArtifact = measured - CHAT_MIN - DIVIDER_WIDTH;
	const desired = preferredArtifactWidth ?? computeDefaultArtifactWidth(measured);
	const artifactWidth = clamp(desired, ARTIFACT_MIN, maxArtifact);
	return { mode: 'split', artifactWidth };
}

function maxArtifactWidth(canvasWidth: number): number {
	return canvasWidth - CHAT_MIN - DIVIDER_WIDTH;
}

/** Dragging the divider left (negative deltaX) grows the artifact pane. */
export function dragArtifactWidth(startWidth: number, deltaX: number, canvasWidth: number): number {
	return clamp(startWidth - deltaX, ARTIFACT_MIN, maxArtifactWidth(canvasWidth));
}

export function keyboardArtifactWidth(current: number, key: string, canvasWidth: number): number | null {
	const max = maxArtifactWidth(canvasWidth);
	if (key === 'ArrowLeft') return clamp(current + KEYBOARD_STEP, ARTIFACT_MIN, max);
	if (key === 'ArrowRight') return clamp(current - KEYBOARD_STEP, ARTIFACT_MIN, max);
	if (key === 'Home') return clamp(ARTIFACT_MIN, ARTIFACT_MIN, max);
	if (key === 'End') return clamp(max, ARTIFACT_MIN, max);
	return null;
}
