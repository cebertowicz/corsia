import { describe, it, expect, beforeEach, vi } from 'vitest';

const initSpy = vi.fn();
const captureSpy = vi.fn();
const identifySpy = vi.fn();
const TEST_API_KEY = 'test-api-key';
const TEST_HOST = 'https://analytics.example.test';

vi.mock('posthog-js', () => ({
	default: {
		init: (...args: unknown[]) => initSpy(...args),
		capture: (...args: unknown[]) => captureSpy(...args),
		identify: (...args: unknown[]) => identifySpy(...args),
	},
}));

import { emitProductEvent, identifyUser, _resetForTests } from './product_events';

interface StubEnv {
	PUBLIC_POSTHOG_API_KEY?: string;
	PUBLIC_POSTHOG_HOST?: string;
	PUBLIC_POSTHOG_PROJECT?: string;
}

function stubEnv(env: StubEnv): void {
	for (const [k, v] of Object.entries(env)) {
		(import.meta.env as Record<string, unknown>)[k] = v;
	}
}

function clearEnv(): void {
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_API_KEY;
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_HOST;
	delete (import.meta.env as Record<string, unknown>).PUBLIC_POSTHOG_PROJECT;
}

beforeEach(() => {
	initSpy.mockReset();
	captureSpy.mockReset();
	identifySpy.mockReset();
	_resetForTests();
	clearEnv();
});

describe('emitProductEvent — no endpoint configuration', () => {
	it('does not call posthog.init when PUBLIC_POSTHOG_API_KEY is unset', () => {
		emitProductEvent('test', 'noop', { foo: 'bar' });
		expect(initSpy).not.toHaveBeenCalled();
		expect(captureSpy).not.toHaveBeenCalled();
	});

	it('does not call posthog.init when PUBLIC_POSTHOG_HOST is unset', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY });
		emitProductEvent('test', 'noop', { foo: 'bar' });
		expect(initSpy).not.toHaveBeenCalled();
		expect(captureSpy).not.toHaveBeenCalled();
	});

	it('repeated emits do not retry init on the hot path', () => {
		emitProductEvent('test', 'first');
		emitProductEvent('test', 'second');
		emitProductEvent('test', 'third');
		expect(initSpy).not.toHaveBeenCalled();
	});
});

describe('emitProductEvent — fail-silent', () => {
	it('swallows posthog.init throwing', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });
		initSpy.mockImplementation(() => {
			throw new Error('init failed');
		});

		expect(() => emitProductEvent('test', 'init-fail')).not.toThrow();
		expect(captureSpy).not.toHaveBeenCalled();
	});

	it('after init failure, subsequent emits do not retry init', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });
		initSpy.mockImplementation(() => {
			throw new Error('init failed');
		});

		emitProductEvent('test', 'first');
		emitProductEvent('test', 'second');
		emitProductEvent('test', 'third');

		expect(initSpy).toHaveBeenCalledTimes(1);
	});

	it('swallows posthog.capture throwing', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });
		captureSpy.mockImplementation(() => {
			throw new Error('capture failed');
		});

		expect(() => emitProductEvent('test', 'capture-fail', { k: 'v' })).not.toThrow();
		expect(initSpy).toHaveBeenCalledTimes(1);
		expect(captureSpy).toHaveBeenCalledTimes(1);
	});
});

describe('emitProductEvent — happy path', () => {
	it('initializes posthog with api_key + host and emits event with project tag', () => {
		stubEnv({
			PUBLIC_POSTHOG_API_KEY: TEST_API_KEY,
			PUBLIC_POSTHOG_HOST: TEST_HOST,
			PUBLIC_POSTHOG_PROJECT: 'corsia',
		});

		emitProductEvent('turn', 'started', { model: 'claude-haiku-4-5', token_count: 142 });

		expect(initSpy).toHaveBeenCalledTimes(1);
		const [token, config] = initSpy.mock.calls[0];
		expect(token).toBe(TEST_API_KEY);
		expect(config).toMatchObject({
			api_host: TEST_HOST,
			capture_pageview: false,
			autocapture: false,
		});

		expect(captureSpy).toHaveBeenCalledTimes(1);
		const [eventName, props] = captureSpy.mock.calls[0];
		expect(eventName).toBe('turn.started');
		expect(props).toMatchObject({
			project: 'corsia',
			model: 'claude-haiku-4-5',
			token_count: 142,
		});
	});

	it('defaults project to corsia when endpoint configuration is explicit', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });

		emitProductEvent('session', 'started');

		const [, props] = captureSpy.mock.calls[0];
		expect(props.project).toBe('corsia');
	});

	it('initializes once across multiple emits (singleton)', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });

		emitProductEvent('a', 'one');
		emitProductEvent('b', 'two');
		emitProductEvent('c', 'three');

		expect(initSpy).toHaveBeenCalledTimes(1);
		expect(captureSpy).toHaveBeenCalledTimes(3);
	});

	it('identifyUser delegates to posthog.identify when initialized', () => {
		stubEnv({ PUBLIC_POSTHOG_API_KEY: TEST_API_KEY, PUBLIC_POSTHOG_HOST: TEST_HOST });

		identifyUser('user-steve', { plan: 'pro' });

		expect(identifySpy).toHaveBeenCalledTimes(1);
		expect(identifySpy.mock.calls[0]).toEqual(['user-steve', { plan: 'pro' }]);
	});

	it('identifyUser no-ops when API key missing', () => {
		identifyUser('user-steve');
		expect(identifySpy).not.toHaveBeenCalled();
	});
});
