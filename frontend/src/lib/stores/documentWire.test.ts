import { describe, expect, it } from 'vitest';
import { textDigest } from './documentWire';

describe('document wire digest', () => {
	// Literal vectors printed by the server's own live_text.text_digest, never by a client helper,
	// so a browser test cannot agree with itself while disagreeing with the server.
	it.each([
		['', 'e3b0c44298fc1c14'],
		['alpha beta', '1a989ea86150171c'],
		['naïve 😀 text\r\n', '00b21601287707ad'],
	])('equals the server digest for %j', async (text, server) => {
		expect(await textDigest(text)).toBe(server);
	});
});
