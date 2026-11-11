import { describe, expect, it } from 'vitest';
import { splitFileName } from './fileNameSplit';

describe('splitFileName', () => {
	it('keeps the end of the stem and the extension in the tail', () => {
		const name = 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf';
		const { head, tail } = splitFileName(name);
		expect(tail).toBe('final-v7.pdf');
		expect(head + tail).toBe(name);
	});

	it('cuts an unbroken name with no extension from its last characters', () => {
		const name = 'a'.repeat(60) + 'XYZ12345';
		expect(splitFileName(name)).toEqual({ head: 'a'.repeat(60), tail: 'XYZ12345' });
	});

	it('does not read a long suffix as an extension', () => {
		const name = 'report-for-the-whole-season.verylongextension';
		const { head, tail } = splitFileName(name);
		expect(tail).toBe('xtension');
		expect(head + tail).toBe(name);
	});

	it('returns a name that is mostly extension whole', () => {
		expect(splitFileName('a.verylong')).toEqual({ head: 'a.verylong', tail: '' });
		expect(splitFileName('x.jpeg')).toEqual({ head: 'x.jpeg', tail: '' });
	});

	it('keeps a leading-dot name as a stem, not an extension', () => {
		const name = '.' + 'b'.repeat(40) + 'end12345';
		const { head, tail } = splitFileName(name);
		expect(tail).toBe('end12345');
		expect(head + tail).toBe(name);
	});

	it('returns a short name whole', () => {
		expect(splitFileName('waiver.pdf')).toEqual({ head: 'waiver.pdf', tail: '' });
		expect(splitFileName('')).toEqual({ head: '', tail: '' });
	});

	it('never splits inside a character made of two code units', () => {
		const name = '\u{1F332}'.repeat(30) + 'trail-map.png';
		const { head, tail } = splitFileName(name);
		expect(tail).toBe('trail-map.png'.slice(-12));
		expect(head + tail).toBe(name);
		expect(Array.from(head).every((c) => c === '\u{1F332}' || c.length === 1)).toBe(true);
	});
});
