import { describe, expect, it } from 'vitest';
import { subjectForRegion } from './diagramSubject';

describe('diagram subject', () => {
	it('includes every element overlapping the original comment box', () => {
		const box = { x: 0.4, y: 0.4, w: 0.2, h: 0.2 };
		const subject = subjectForRegion(box, [
			{ x: 0.3, y: 0.45, w: 0.2, h: 0.1 },
			{ x: 0.55, y: 0.35, w: 0.2, h: 0.3 },
			{ x: 0.76, y: 0.4, w: 0.1, h: 0.1 },
		]);
		expect(subject.x).toBeCloseTo(0.3);
		expect(subject.y).toBeCloseTo(0.35);
		expect(subject.w).toBeCloseTo(0.45);
		expect(subject.h).toBeCloseTo(0.3);
	});
	it('keeps the original box when nothing overlaps', () => {
		const box = { x: 0.1, y: 0.2, w: 0.1, h: 0.1 };
		const subject = subjectForRegion(box, []);
		expect(subject.x).toBeCloseTo(box.x);
		expect(subject.y).toBeCloseTo(box.y);
		expect(subject.w).toBeCloseTo(box.w);
		expect(subject.h).toBeCloseTo(box.h);
	});
});
