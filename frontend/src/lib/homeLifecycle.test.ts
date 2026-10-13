import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { registerHomeLifecycle } from './homeLifecycle';

describe('registerHomeLifecycle polling', () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});

	afterEach(() => {
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	it('reads update state on reconnect while leaving operation polling to the update store', () => {
		const listeners = new Map<string, (data: any) => void>();
		const loadThreads = vi.fn();
		const loadModels = vi.fn();
		const loadSettings = vi.fn();
		const loadUpdateStatus = vi.fn();
		const pollSystem = vi.fn();
		const checkSecurityAlert = vi.fn();
		const teardown = registerHomeLifecycle({
			token: () => null,
			activeThreadId: () => null,
			activeView: () => 'activity',
			threads: () => [],
			loadThreads,
			loadModels,
			loadSettings,
			loadUpdateStatus,
			pollSystem,
			checkSecurityAlert,
			selectThread: vi.fn(async () => {}),
			setActiveView: vi.fn(),
			chatPanel: () => undefined,
			on: (event, callback) => {
				listeners.set(event, callback);
				return () => listeners.delete(event);
			},
			connect: vi.fn(),
		});

		listeners.get('connected')?.(undefined);
		listeners.get('connected')?.(undefined);

		expect(loadThreads).toHaveBeenCalledTimes(2);
		expect(loadModels).toHaveBeenCalledTimes(2);
		expect(loadSettings).toHaveBeenCalledTimes(2);
		expect(loadUpdateStatus).toHaveBeenCalledTimes(2);
		expect(pollSystem).toHaveBeenCalledTimes(2);
		expect(checkSecurityAlert).toHaveBeenCalledTimes(2);
		expect(vi.getTimerCount()).toBe(1);

		vi.advanceTimersByTime(30_000);

		expect(loadSettings).toHaveBeenCalledTimes(3);
		expect(loadUpdateStatus).toHaveBeenCalledTimes(2);
		expect(pollSystem).toHaveBeenCalledTimes(3);
		expect(loadThreads).toHaveBeenCalledTimes(2);
		expect(loadModels).toHaveBeenCalledTimes(2);
		expect(checkSecurityAlert).toHaveBeenCalledTimes(2);

		teardown();
		expect(vi.getTimerCount()).toBe(0);
	});
});
