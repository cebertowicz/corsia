import { lineDiff, diffStats } from './diff';

describe('lineDiff', () => {
	it('marks unchanged lines as ctx', () => {
		const d = lineDiff('a\nb\nc', 'a\nb\nc')!;
		expect(d.every((l) => l.type === 'ctx')).toBe(true);
		expect(d.length).toBe(3);
	});

	it('detects a changed line as del+add around context', () => {
		const d = lineDiff('a\nx = 1\nb', 'a\nx = 2\nb')!;
		expect(d.map((l) => `${l.type}:${l.text}`)).toEqual([
			'ctx:a', 'del:x = 1', 'add:x = 2', 'ctx:b',
		]);
	});

	it('pure insertion (write-tool preview) is all adds', () => {
		const d = lineDiff('', 'one\ntwo')!;
		expect(d.map((l) => l.type)).toEqual(['add', 'add']);
	});

	it('pure deletion is all dels', () => {
		const d = lineDiff('one\ntwo', '')!;
		expect(d.map((l) => l.type)).toEqual(['del', 'del']);
	});

	it('bails out above the line cap', () => {
		const big = Array.from({ length: 401 }, (_, i) => `l${i}`).join('\n');
		expect(lineDiff(big, 'x')).toBeNull();
	});

	it('diffStats counts adds and dels', () => {
		const d = lineDiff('a\nb', 'a\nc\nd')!;
		expect(diffStats(d)).toEqual([2, 1]);
	});
});
