import { describe, expect, it, vi } from 'vitest';

import { handleMessage } from './handlers';
import { on } from './events';

describe('host-access websocket hints', () => {
	it('passes routing identity through without treating payload state as truth', () => {
		const listener = vi.fn();
		const stop = on('host_access_changed', listener);
		const frame = {
			type: 'host_access_changed',
			lease_id: 'lease-1',
			lease: { state: 'active', purpose: 'untrusted' },
		};

		try {
			handleMessage(frame, vi.fn());
		} finally {
			stop();
		}

		expect(listener).toHaveBeenCalledOnce();
		expect(listener).toHaveBeenCalledWith(frame);
	});
});
