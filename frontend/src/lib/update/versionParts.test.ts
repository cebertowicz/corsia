import { describe, expect, it } from 'vitest';
import { headlineParts } from './versionParts';

describe('headlineParts', () => {
	it('finds each version and leaves the trailing full stop as text', () => {
		expect(headlineParts('CORSIA is on 0.4.3-dev.1288, not 0.4.3-dev.1311.')).toEqual([
			{ text: 'CORSIA is on ', version: false },
			{ text: '0.4.3-dev.1288', version: true },
			{ text: ', not ', version: false },
			{ text: '0.4.3-dev.1311', version: true },
			{ text: '.', version: false },
		]);
	});

	it('finds a plain version', () => {
		expect(headlineParts('CORSIA is on 2026.10.13.')).toEqual([
			{ text: 'CORSIA is on ', version: false },
			{ text: '2026.10.13', version: true },
			{ text: '.', version: false },
		]);
	});

	it('returns one text part when there is no version', () => {
		expect(headlineParts('Couldn’t start the new version.')).toEqual([{ text: 'Couldn’t start the new version.', version: false }]);
	});
});
