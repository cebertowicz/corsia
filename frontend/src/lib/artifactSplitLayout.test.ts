import { describe, expect, it } from 'vitest';
import {
	computeSplitLayout, computeDefaultArtifactWidth, dragArtifactWidth, keyboardArtifactWidth,
	clamp, shouldOverlayPane, CHAT_MIN, ARTIFACT_MIN, DIVIDER_WIDTH, COMBINED_MIN,
	PANE_DRAWER_WIDTH, ARTIFACT_DEFAULT_MIN_CAP, ARTIFACT_DEFAULT_MAX_CAP,
} from './artifactSplitLayout';

describe('shouldOverlayPane', () => {
	it('overlays on desktop exactly below the canonical combined minimum', () => {
		expect(shouldOverlayPane(true, 1025, COMBINED_MIN - 1)).toBe(true);
		expect(shouldOverlayPane(true, 1025, COMBINED_MIN)).toBe(false);
	});

	it('follows the actual available canvas width as the sidebar expands or collapses', () => {
		const expandedSidebarCanvas = COMBINED_MIN - 1;
		const collapsedSidebarCanvas = COMBINED_MIN;

		expect(shouldOverlayPane(true, 1440, expandedSidebarCanvas)).toBe(true);
		expect(shouldOverlayPane(true, 1440, collapsedSidebarCanvas)).toBe(false);
	});

	it('preserves the existing compact drawer path at and below 1024px', () => {
		expect(shouldOverlayPane(true, 1024, COMBINED_MIN - 1)).toBe(false);
		expect(shouldOverlayPane(true, 768, COMBINED_MIN - 1)).toBe(false);
	});

	it('does not make an unmeasured canvas overlay', () => {
		expect(shouldOverlayPane(true, 1440, 0)).toBe(false);
	});

	it('never overlays an ordinary drawer when no visible artifact leaf needs the split minimum', () => {
		expect(shouldOverlayPane(false, 1440, COMBINED_MIN - 1)).toBe(false);
	});

	it('keeps the drawer-width conversion explicit for the measured shell', () => {
		expect(PANE_DRAWER_WIDTH).toBe(312);
	});
});

describe('computeSplitLayout', () => {
	it('is chat-only when there are no tabs, regardless of width or visibility', () => {
		expect(computeSplitLayout(1400, false, true, null)).toEqual({ mode: 'chat-only' });
		expect(computeSplitLayout(1400, false, false, null)).toEqual({ mode: 'chat-only' });
	});

	it('is rail when the leaf is manually collapsed (visible=false), even on a wide canvas', () => {
		expect(computeSplitLayout(1600, true, false, null)).toEqual({ mode: 'rail' });
	});

	it('goes compact below combined minima on a narrow desktop window', () => {
		const combinedMin = CHAT_MIN + ARTIFACT_MIN + DIVIDER_WIDTH;
		expect(computeSplitLayout(combinedMin - 1, true, true, null)).toEqual({ mode: 'compact' });
	});

	it('goes compact on a phone-width canvas', () => {
		expect(computeSplitLayout(390, true, true, null)).toEqual({ mode: 'compact' });
	});

	it('goes compact when the canvas is too narrow even with the leaf collapsed', () => {
		// A rail has no home on a canvas too narrow to split; chat simply owns it.
		expect(computeSplitLayout(390, true, false, null)).toEqual({ mode: 'compact' });
	});

	it('is driven by the canvas, so a wide window with a narrow canvas is compact', () => {
		// e.g. the right drawer open on a desktop window.
		expect(computeSplitLayout(700, true, true, null)).toEqual({ mode: 'compact' });
	});

	it('treats an unmeasured canvas as wide so the split does not flash compact', () => {
		expect(computeSplitLayout(0, true, true, null)).toMatchObject({ mode: 'split' });
	});

	it('splits at exactly the combined minimum, giving the artifact pane its floor width', () => {
		const combinedMin = CHAT_MIN + ARTIFACT_MIN + DIVIDER_WIDTH;
		const layout = computeSplitLayout(combinedMin, true, true, null);
		expect(layout).toEqual({ mode: 'split', artifactWidth: ARTIFACT_MIN });
	});

	it('uses the default ~45% fraction capped to 600-640 when no preference is set', () => {
		const layout = computeSplitLayout(1600, true, true, null);
		expect(layout.mode).toBe('split');
		if (layout.mode === 'split') {
			expect(layout.artifactWidth).toBe(ARTIFACT_DEFAULT_MAX_CAP);
		}
	});

	it('respects a persisted preferred width, clamped to the current canvas', () => {
		const layout = computeSplitLayout(1600, true, true, 500);
		expect(layout).toEqual({ mode: 'split', artifactWidth: 500 });
	});

	it('clamps an oversized preferred width so chat keeps its minimum', () => {
		const canvasWidth = 1000;
		const layout = computeSplitLayout(canvasWidth, true, true, 900);
		expect(layout.mode).toBe('split');
		if (layout.mode === 'split') {
			expect(layout.artifactWidth).toBe(canvasWidth - CHAT_MIN - DIVIDER_WIDTH);
		}
	});

	it('clamps an undersized preferred width up to the artifact minimum', () => {
		const layout = computeSplitLayout(1600, true, true, 100);
		expect(layout).toEqual({ mode: 'split', artifactWidth: ARTIFACT_MIN });
	});
});

describe('computeDefaultArtifactWidth', () => {
	it('is clamped between the min and max caps', () => {
		expect(computeDefaultArtifactWidth(500)).toBe(ARTIFACT_DEFAULT_MIN_CAP);
		expect(computeDefaultArtifactWidth(3000)).toBe(ARTIFACT_DEFAULT_MAX_CAP);
	});

	it('is ~45% of canvas width in the unclamped middle range', () => {
		expect(computeDefaultArtifactWidth(1400)).toBeCloseTo(1400 * 0.45, 5);
	});
});

describe('clamp', () => {
	it('clamps within range and returns min when max < min', () => {
		expect(clamp(5, 0, 10)).toBe(5);
		expect(clamp(-5, 0, 10)).toBe(0);
		expect(clamp(50, 0, 10)).toBe(10);
		expect(clamp(5, 10, 0)).toBe(10);
	});
});

describe('dragArtifactWidth', () => {
	const canvasWidth = 1400;
	it('dragging left (negative deltaX) grows the artifact pane', () => {
		expect(dragArtifactWidth(500, -100, canvasWidth)).toBe(600);
	});
	it('dragging right (positive deltaX) shrinks the artifact pane', () => {
		expect(dragArtifactWidth(500, 100, canvasWidth)).toBe(400);
	});
	it('clamps to the artifact minimum', () => {
		expect(dragArtifactWidth(400, 500, canvasWidth)).toBe(ARTIFACT_MIN);
	});
	it('clamps to the max that preserves chat minimum', () => {
		expect(dragArtifactWidth(500, -900, canvasWidth)).toBe(canvasWidth - CHAT_MIN - DIVIDER_WIDTH);
	});
});

describe('keyboardArtifactWidth', () => {
	const canvasWidth = 1400;
	it('ArrowLeft grows, ArrowRight shrinks by the step', () => {
		expect(keyboardArtifactWidth(500, 'ArrowLeft', canvasWidth)).toBe(524);
		expect(keyboardArtifactWidth(500, 'ArrowRight', canvasWidth)).toBe(476);
	});
	it('Home jumps to the artifact minimum, End jumps to the max', () => {
		expect(keyboardArtifactWidth(500, 'Home', canvasWidth)).toBe(ARTIFACT_MIN);
		expect(keyboardArtifactWidth(500, 'End', canvasWidth)).toBe(canvasWidth - CHAT_MIN - DIVIDER_WIDTH);
	});
	it('returns null for unrelated keys', () => {
		expect(keyboardArtifactWidth(500, 'Tab', canvasWidth)).toBeNull();
	});
});
