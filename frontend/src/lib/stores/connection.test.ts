import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { checkSecurityAlert, securityAlert } from './connection';

describe('security alert connection check', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		securityAlert.set(null);
	});

	it('uses the lifecycle token for the authenticated alert request', async () => {
		const fetchMock = vi.fn(async () => ({
			ok: true,
			json: async () => ({ vulns: 1, errors: 0, report: 'report', date: 'today' }),
		}));
		vi.stubGlobal('fetch', fetchMock);

		const authToken = 'session token';
		await checkSecurityAlert(authToken);

		expect(fetchMock).toHaveBeenCalledWith('/api/security/alert', {
			headers: { Authorization: `Bearer ${authToken}` },
		});
		expect(fetchMock.mock.calls[0][0]).not.toContain('?');
		expect(get(securityAlert)).toMatchObject({ vulns: 1, errors: 0 });
	});

	it('clears the alert without a request when no lifecycle token is available', async () => {
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		securityAlert.set({ vulns: 1, errors: 0, report: 'report', date: 'today' });

		await checkSecurityAlert(null);

		expect(fetchMock).not.toHaveBeenCalled();
		expect(get(securityAlert)).toBeNull();
	});
});
