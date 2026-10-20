import { describe, expect, it } from 'vitest';
import { saveFailureText } from './saveFailure';

const PERSON_ONLY = 'Only the signed-in person can change where models run. Change it in CORSIA while signed in.';

describe('saveFailureText', () => {
	it('adds a sentence boundary after the server reason', () => {
		expect(saveFailureText('Your permission changed', true)).toBe('Couldn’t save this change. Your permission changed.');
	});

	it('shows a server reason with its own next step, without telling the person to retry', () => {
		expect(saveFailureText(PERSON_ONLY, true)).toBe(`Couldn’t save this change. ${PERSON_ONLY}`);
	});

	it('uses the plain line for missing or machine-readable reasons', () => {
		expect(saveFailureText(undefined, true)).toBe('Couldn’t save this change. Try again.');
		expect(saveFailureText('provider_write_outcome_unknown', true)).toBe('Couldn’t save this change. Try again.');
	});

	it('gives the connection recovery when CORSIA was never reached', () => {
		expect(saveFailureText(undefined, false)).toBe('Couldn’t save this change. Check your connection and try again.');
	});
});
