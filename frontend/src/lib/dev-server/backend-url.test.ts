import { describe, expect, it } from 'vitest';
import { readBackendUrl, requireBackendUrl } from './backend-url';

describe('requireBackendUrl', () => {
	it('throws when unset or blank, naming the variable to export', () => {
		expect(() => requireBackendUrl({})).toThrow(/export CORSIA_BACKEND_URL=/);
		expect(() => requireBackendUrl({ CORSIA_BACKEND_URL: '  ' })).toThrow(
			/CORSIA_BACKEND_URL is not set/,
		);
	});

	it('uses the value when set', () => {
		expect(requireBackendUrl({ CORSIA_BACKEND_URL: 'http://127.0.0.1:3110' })).toBe(
			'http://127.0.0.1:3110',
		);
	});
});

describe('readBackendUrl', () => {
	it('never throws and never invents a default', () => {
		expect(readBackendUrl({})).toBeUndefined();
		expect(readBackendUrl({ CORSIA_BACKEND_URL: 'http://127.0.0.1:3110' })).toBe(
			'http://127.0.0.1:3110',
		);
	});
});
