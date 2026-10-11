import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { clusterMarkerLines, COARSE_RAIL, FINE_RAIL, markerRailWidth, readPointerEnvironment } from './comment-clusters';

const line = (top: number, ...ids: string[]) => ({ top, ids });

describe('clusterMarkerLines', () => {
	it('gives each line its own marker when rows are a target height apart or more', () => {
		expect(clusterMarkerLines([line(0, 'a'), line(26, 'b'), line(52, 'c')], FINE_RAIL)).toEqual([
			{ top: 0, ids: ['a'] }, { top: 26, ids: ['b'] }, { top: 52, ids: ['c'] },
		]);
	});

	it('keeps two threads of one line in one cluster', () => {
		expect(clusterMarkerLines([line(10, 'a', 'b')], FINE_RAIL)).toEqual([{ top: 10, ids: ['a', 'b'] }]);
	});

	it('joins a row less than a target height below the anchor, not one exactly a height below', () => {
		expect(clusterMarkerLines([line(0, 'a'), line(43.9, 'b')], COARSE_RAIL)).toEqual([{ top: 0, ids: ['a', 'b'] }]);
		expect(clusterMarkerLines([line(0, 'a'), line(44, 'b')], COARSE_RAIL)).toHaveLength(2);
	});

	it('measures from the anchor, so a run of close rows does not chain across paragraphs', () => {
		const rows = [0, 27, 54, 81, 108, 135].map((top, index) => line(top, `t${index}`));
		const clusters = clusterMarkerLines(rows, COARSE_RAIL);
		expect(clusters.map((cluster) => cluster.ids)).toEqual([['t0', 't1'], ['t2', 't3'], ['t4', 't5']]);
		expect(clusters.map((cluster) => cluster.top)).toEqual([0, 54, 108]);
	});

	it('lists each id once, in document order, across every cluster', () => {
		const clusters = clusterMarkerLines([line(0, 'a', 'b'), line(10, 'b', 'c'), line(100, 'c', 'd'), line(300, 'a', 'e')], COARSE_RAIL);
		expect(clusters.map((cluster) => cluster.ids)).toEqual([['a', 'b', 'c'], ['d'], ['e']]);
		expect(clusters.flatMap((cluster) => cluster.ids)).toEqual(['a', 'b', 'c', 'd', 'e']);
	});

	it('loses no id in a dense run and keeps two-digit counts whole', () => {
		const ids = Array.from({ length: 12 }, (_, index) => `m${index}`);
		const [only, ...rest] = clusterMarkerLines([line(0, ...ids)], FINE_RAIL);
		expect(rest).toEqual([]);
		expect(only.ids).toHaveLength(12);
		const dense = Array.from({ length: 120 }, (_, index) => line(index * 26, `d${index}`));
		expect(clusterMarkerLines(dense, COARSE_RAIL).flatMap((cluster) => cluster.ids)).toEqual(dense.flatMap((row) => row.ids));
	});

	it('lists a thread once even when two of its ranges share a line', () => {
		expect(clusterMarkerLines([line(0, 'a', 'a', 'b')], FINE_RAIL)).toEqual([{ top: 0, ids: ['a', 'b'] }]);
	});

	it('skips a line whose threads were all placed already, so it never becomes an empty marker', () => {
		expect(clusterMarkerLines([line(0, 'a'), line(200, 'a')], FINE_RAIL)).toEqual([{ top: 0, ids: ['a'] }]);
	});
});

describe('markerRailWidth', () => {
	it('is 24px for a fine pointer with no touch', () => {
		expect(markerRailWidth({ coarse: false, touchPoints: 0, touchEvents: false })).toBe(FINE_RAIL);
	});

	it('is 44px when any pointer is coarse', () => {
		expect(markerRailWidth({ coarse: true, touchPoints: 0, touchEvents: false })).toBe(COARSE_RAIL);
	});

	it('is 44px when touch is present even though no pointer reports coarse', () => {
		expect(markerRailWidth({ coarse: false, touchPoints: 5, touchEvents: false })).toBe(COARSE_RAIL);
		expect(markerRailWidth({ coarse: false, touchPoints: 0, touchEvents: true })).toBe(COARSE_RAIL);
	});

	it('is 44px when nothing about the pointer is known', () => {
		expect(markerRailWidth({})).toBe(COARSE_RAIL);
	});

	it('stays 24px when only the touch count is unknown but the media query says fine', () => {
		expect(markerRailWidth({ coarse: false })).toBe(FINE_RAIL);
	});

	it('reads the environment from the window it is given', () => {
		const win = { matchMedia: (query: string) => ({ matches: query === '(any-pointer: coarse)' }), navigator: { maxTouchPoints: 0 } } as unknown as Window;
		expect(readPointerEnvironment(win)).toEqual({ coarse: true, touchPoints: 0, touchEvents: false });
		expect(readPointerEnvironment({ navigator: {} } as unknown as Window)).toEqual({ coarse: undefined, touchPoints: undefined, touchEvents: false });
	});
});

describe('phone text inset', () => {
	// The header and review bar are Svelte CSS and cannot import FINE_RAIL; the editor theme does. They must agree on the inset.
	const strip = (source: string) => source.replace(/\/\*[\s\S]*?\*\//g, '');
	const component = (name: string) => strip(readFileSync(resolve(__dirname, '../components', name), 'utf8'));

	it('is the fine rail in the document header and the review bar, as in the editor theme', () => {
		expect(component('DocViewerStatus.svelte')).toContain(`.document-header { padding-left: ${FINE_RAIL}px; }`);
		expect(component('ReviewBar.svelte')).toContain(`.review-bar { margin-left: -${FINE_RAIL}px; }`);
		expect(strip(readFileSync(resolve(__dirname, 'theme.ts'), 'utf8'))).toContain('paddingInline: `${FINE_RAIL}px`');
	});
});
