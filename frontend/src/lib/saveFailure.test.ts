import { describe, expect, it } from 'vitest';
import { saveFailureText } from './saveFailure';

describe('saveFailureText', () => {
	it('adds a sentence boundary before the recovery step', () => {
		expect(saveFailureText('Your permission changed', true)).toBe("Couldn't save this change. Your permission changed. Try again.");
	});

	it('keeps a server-written retry step without adding another', () => {
		expect(saveFailureText('Check the project settings and try again.', true)).toBe("Couldn't save this change. Check the project settings and try again.");
	});

	it('uses the plain line for missing or machine-readable reasons', () => {
		expect(saveFailureText(undefined, true)).toBe("Couldn't save this change. Try again.");
		expect(saveFailureText('person_session_required', true)).toBe("Couldn't save this change. Try again.");
	});

	it('gives the connection recovery when CORSIA was never reached', () => {
		expect(saveFailureText(undefined, false)).toBe("Couldn't save this change. Check your connection and try again.");
	});
});
