import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DocComment } from './docComments';
import { clearReloadErrors, markReloading, refuseRequest, reloadFlags, reportReloadFailure, settleReload } from './docCommentReload';

const row = (patch: Partial<DocComment>) => ({ id: 'c1', anchor: 'quote', author: 'you', text: 'c1', at: 'now', resolved: false, replies: [], ...patch }) as DocComment;

afterEach(() => { vi.restoreAllMocks(); });

describe('placement reload flags', () => {
	it('carries a reload in flight or refused through a fresh load, and nothing otherwise', () => {
		expect(reloadFlags(undefined)).toEqual({});
		expect(reloadFlags(row({}))).toEqual({});
		expect(reloadFlags(row({ placementReloading: true }))).toEqual({ placementReloading: true, placementReloadError: undefined });
		expect(reloadFlags(row({ placementReloadError: true }))).toEqual({ placementReloading: undefined, placementReloadError: true });
	});

	it('marks anchored rows Placing, keeps a never-drawn row unplaced, and leaves review comments and unanchored rows alone', () => {
		const [drawn, neverDrawn, review, bare] = markReloading([
			row({ id: 'a', placement: 'placed', drawn: true, placementReloadError: true }),
			row({ id: 'b', placement: undefined, drawn: false }),
			row({ id: 'c', review: true }),
			row({ id: 'd', anchor: '' }),
		]);
		expect(drawn).toMatchObject({ placement: 'placing', placementReloading: true, placementReloadError: false });
		expect(neverDrawn).toMatchObject({ placement: undefined, placementReloading: true, placementReloadError: false });
		expect(review.placementReloading).toBeUndefined();
		expect(bare.placementReloading).toBeUndefined();
	});

	it('leaves rows still Placing unplaced on a refusal, and marks a refused reload on its rows', () => {
		const rows = [row({ id: 'a', placement: 'placing', placementReloading: true }), row({ id: 'b', placement: 'placed', placementReloading: true })];
		expect(refuseRequest(rows, true)).toMatchObject([
			{ placement: 'not_placed', placementReloadError: true },
			{ placement: 'placed', placementReloadError: true },
		]);
		expect(refuseRequest(rows, false)).toMatchObject([{ placement: 'not_placed', placementReloadError: undefined }, { placement: 'placed' }]);
		expect(refuseRequest(rows, false)[1]).toBe(rows[1]);
	});

	it('clears a refusal once answered and settles the reload, returning the same rows when there is nothing to change', () => {
		const rows = [row({ placementReloading: true, placementReloadError: true })];
		expect(clearReloadErrors(rows)).toMatchObject([{ placementReloading: true, placementReloadError: false }]);
		expect(settleReload(rows)).toMatchObject([{ placementReloading: false, placementReloadError: true }]);
		const idle = [row({})];
		expect(clearReloadErrors(idle)).toBe(idle);
		expect(settleReload(idle)).toBe(idle);
	});

	it('records a refused reload once, naming the document and the refusal', () => {
		const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
		reportReloadFailure('artifact', 'refused');
		expect(errors).toHaveBeenCalledTimes(1);
		expect(errors).toHaveBeenCalledWith('Comment placement: placing lost comments again failed', { artifactId: 'artifact', kind: 'refused' });
	});
});
