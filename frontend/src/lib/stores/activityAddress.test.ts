import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { activityOpenRequest } from './activityOpen';
import { listenForOpenMessages, openActivityFromAddress, OPEN_MESSAGE } from './activityAddress';

describe('openActivityFromAddress', () => {
	afterEach(() => { activityOpenRequest.set(null); window.history.replaceState(null, '', '/'); window.sessionStorage.clear(); });

	it('opens the named item and cleans the address, keeping other keys', () => {
		window.history.replaceState(null, '', '/?activity=inv-2291&view=x#top');
		openActivityFromAddress();
		expect(get(activityOpenRequest)).toEqual({ externalId: 'inv-2291' });
		expect(window.location.search).toBe('?view=x');
		expect(window.location.hash).toBe('#top');
	});

	it('does nothing without the key, so a reload after cleaning opens nothing', () => {
		window.history.replaceState(null, '', '/');
		openActivityFromAddress();
		expect(get(activityOpenRequest)).toBeNull();
	});

	it('an empty id opens Activity on its top item', () => {
		window.history.replaceState(null, '', '/?activity=');
		openActivityFromAddress();
		expect(get(activityOpenRequest)).toEqual({ externalId: null });
		expect(window.location.search).toBe('');
	});

	it('survives a real reload before Activity takes it, and is gone once Activity has opened it', async () => {
		window.history.replaceState(null, '', '/?activity=inv-2291');
		openActivityFromAddress();
		// A reload during sign-in: every module is new (nothing in memory), the address is clean, the tab's storage stays.
		vi.resetModules();
		window.history.replaceState(null, '', '/');
		expect(window.sessionStorage.getItem('corsia_activity_address')).toBe('inv-2291');
		const reloaded = await import('./activityAddress');
		const request = (await import('./activityOpen')).activityOpenRequest;
		expect(get(request)).toBeNull();
		reloaded.openActivityFromAddress();
		expect(get(request)).toEqual({ externalId: 'inv-2291' });
		// Activity opens it and clears the request: the stored copy goes, so the next reload opens nothing.
		request.set(null);
		expect(window.sessionStorage.getItem('corsia_activity_address')).toBeNull();
		reloaded.openActivityFromAddress();
		expect(get(request)).toBeNull();
	});

	it('an id with characters outside the route is ignored', () => {
		window.history.replaceState(null, '', '/?activity=%2F%2Fevil.example');
		openActivityFromAddress();
		expect(get(activityOpenRequest)).toBeNull();
		expect(window.sessionStorage.getItem('corsia_activity_address')).toBeNull();
	});
});

describe('listenForOpenMessages', () => {
	const fake = () => {
		const handlers: ((event: { data: unknown }) => void)[] = [];
		let started = 0;
		Object.defineProperty(navigator, 'serviceWorker', {
			configurable: true,
			value: {
				addEventListener: (_type: string, handler: (event: { data: unknown }) => void) => { handlers.push(handler); },
				removeEventListener: () => undefined,
				startMessages: () => { started += 1; },
			},
		});
		return { handlers, started: () => started };
	};
	afterEach(() => { activityOpenRequest.set(null); window.sessionStorage.clear(); });

	it('opens the item the worker names, starts the message queue, and ignores anything else', () => {
		const sw = fake();
		listenForOpenMessages();
		expect(sw.started()).toBe(1);
		const send = (data: unknown) => sw.handlers[0]({ data });
		send({ type: OPEN_MESSAGE, path: '//evil.example' });
		send({ type: OPEN_MESSAGE, path: '/\\evil.example' });
		send({ type: 'other', path: '/?activity=a' });
		send(null);
		expect(get(activityOpenRequest)).toBeNull();
		send({ type: OPEN_MESSAGE, path: '/?activity=inv-2291' });
		expect(get(activityOpenRequest)).toEqual({ externalId: 'inv-2291' });
	});
});
