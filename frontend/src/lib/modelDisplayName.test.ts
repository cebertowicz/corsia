import { describe, expect, it } from 'vitest';
import parity from '../../../testdata/model-display-names.json';
import { externalModelDisplayName } from './modelDisplayName';

describe('external model display names, shared with the backend', () => {
	it.each(parity.cases)('$name', ({ value, expected }) => {
		expect(externalModelDisplayName(value)).toBe(expected);
	});
});
