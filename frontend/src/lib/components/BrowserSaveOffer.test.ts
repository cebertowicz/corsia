import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';
import { shortenMiddle } from '$lib/browserSaveOfferText';

const reducedMotion = vi.hoisted(() => ({ value: null as null | { set(value: boolean): void } }));
vi.mock('$lib/stores/viewport', async (importOriginal) => {
	const { writable: store } = await import('svelte/store');
	const prefersReducedMotion = store(false);
	reducedMotion.value = prefersReducedMotion;
	return { ...(await importOriginal<object>()), prefersReducedMotion };
});


class FakeSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSED = 3;
	static instances: FakeSocket[] = [];
	readyState = FakeSocket.CONNECTING;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	sent: Record<string, unknown>[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sent.push(JSON.parse(raw)); }
	close() { this.readyState = FakeSocket.CLOSED; this.onclose?.(); }
	open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
	receive(message: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(message) }); }
}

const OFFER = { site: 'https://github.com', domain: 'github.com', username: 'jordan@example.com', update: false };
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let offers: Array<typeof OFFER | null>;
let saveStatus: number;
let calls: string[];

const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); flushSync(); };
const dock = () => host.querySelector<HTMLElement>('.save-offer');
const liveText = () => [...host.querySelectorAll('[aria-live="polite"]')].map(node => node.textContent).join('|');
const button = (name: string) => [...host.querySelectorAll<HTMLButtonElement>('.save-offer button')].find(node => node.textContent?.trim() === name)!;

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
	flushSync();
	const socket = FakeSocket.instances[0];
	socket.open();
	socket.receive({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' });
	return { canvas: host.querySelector<HTMLCanvasElement>('canvas')!, socket };
}

const typeKey = (canvas: HTMLCanvasElement) => canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', code: 'KeyA', bubbles: true, cancelable: true }));
const passwordPage = (socket: FakeSocket, visible: boolean) => { socket.receive({ type: 'browser_password_page', visible }); flushSync(); };

async function signIn(socket: FakeSocket, canvas: HTMLCanvasElement) {
	passwordPage(socket, true);
	typeKey(canvas);
	passwordPage(socket, false);
	await settle();
}

beforeEach(() => {
	FakeSocket.instances = [];
	offers = [OFFER];
	saveStatus = 200;
	calls = [];
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('Image', class { src = ''; decode() { return Promise.resolve(); } });
	vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
		calls.push(`${init?.method ?? 'GET'} ${input}${init?.body ? ` ${init.body}` : ''}`);
		if (input.startsWith('/api/saved-logins/offer?')) return { ok: true, status: 200, json: async () => ({ offer: offers.length > 1 ? offers.shift() : offers[0] ?? null }) };
		if (input.endsWith('/save')) return { ok: saveStatus === 200, status: saveStatus, json: async () => ({ saved: true }) };
		return { ok: true, status: 200, json: async () => ({}) };
	}));
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
	vi.useRealTimers();
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

const gets = () => calls.filter(call => call.startsWith('GET /api/saved-logins/offer'));

describe('save-login offer: when it asks', () => {
	it('never asks when no one typed, as when the agent signed in alone', async () => {
		const { socket } = render();
		passwordPage(socket, true);
		passwordPage(socket, false);
		await settle();
		expect(gets()).toEqual([]);
		expect(dock()).toBeNull();
	});

	it('does not count typing outside a password page', async () => {
		const { socket, canvas } = render();
		typeKey(canvas);
		passwordPage(socket, true);
		passwordPage(socket, false);
		await settle();
		expect(gets()).toEqual([]);
	});

	it('asks once when the password page goes away after the person typed, and shows the offer', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(gets()).toEqual(['GET /api/saved-logins/offer?thread_id=thread']);
		expect(dock()!.textContent).toContain('Save the github.com login for agents?');
		expect(dock()!.textContent).toContain('jordan@example.com');
		expect(button('Save')).toBeTruthy();
		expect(button('Not now')).toBeTruthy();
	});

	it('counts a paste as typing', async () => {
		const { socket } = render();
		passwordPage(socket, true);
		const paste = Object.assign(new Event('paste', { bubbles: true, cancelable: true }), { clipboardData: { getData: () => 'pasted' } });
		host.querySelector('textarea')!.dispatchEvent(paste);
		passwordPage(socket, false);
		await settle();
		expect(gets()).toHaveLength(1);
	});

	it('asks once more on the next navigate_result when the first answer was empty, and never polls', async () => {
		offers = [null, OFFER];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()).toBeNull();
		expect(gets()).toHaveLength(1);
		socket.receive({ type: 'navigate_result', url: 'https://github.com/home', title: 'Home', domain: 'github.com' });
		await settle();
		expect(gets()).toHaveLength(2);
		expect(dock()).not.toBeNull();
		socket.receive({ type: 'navigate_result', url: 'https://github.com/other', title: 'Other', domain: 'github.com' });
		await settle();
		expect(gets()).toHaveLength(2);
	});

	it('does not ask again on navigate_result after an empty second answer', async () => {
		offers = [null];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		socket.receive({ type: 'navigate_result', url: 'https://github.com/a', title: 'A', domain: 'github.com' });
		await settle();
		socket.receive({ type: 'navigate_result', url: 'https://github.com/b', title: 'B', domain: 'github.com' });
		await settle();
		expect(gets()).toHaveLength(2);
	});
});

describe('save-login offer: a page that navigates by itself', () => {
	const metadata = (socket: FakeSocket, domain: string) => socket.receive({ type: 'browser_metadata', url: `https://${domain}/x`, title: 'T', domain });

	it('closes when browser_metadata names another site, and stays for the same one', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		metadata(socket, 'github.com');
		await settle();
		expect(dock()).not.toBeNull();
		metadata(socket, 'example.org');
		await settle();
		expect(dock()).toBeNull();
	});

	it('asks the one retry on the next browser_metadata that carries a domain, and not again', async () => {
		offers = [null, OFFER];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(gets()).toHaveLength(1);
		socket.receive({ type: 'browser_metadata', url: 'https://github.com/x', title: 'T' });
		await settle();
		expect(gets()).toHaveLength(1);
		metadata(socket, 'github.com');
		await settle();
		expect(gets()).toHaveLength(2);
		expect(dock()).not.toBeNull();
	});

	it('spends the retry once across browser_metadata and navigate_result together', async () => {
		offers = [null];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		metadata(socket, 'github.com');
		await settle();
		socket.receive({ type: 'navigate_result', url: 'https://github.com/y', title: 'Y', domain: 'github.com' });
		await settle();
		metadata(socket, 'github.com');
		await settle();
		expect(gets()).toHaveLength(2);
	});
});

describe('save-login offer: what it shows', () => {
	it('shows the whole domain, however long, when the question is not measured to need more than two lines', async () => {
		const long = `${'x'.repeat(60)}.example.com`;
		offers = [{ ...OFFER, domain: long }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.querySelector('.question')!.textContent).toContain(long);
	});

	it('shows Save in its loading state while the POST runs', async () => {
		let release: (value: unknown) => void = () => {};
		const base = fetch as unknown as (input: string, init?: RequestInit) => Promise<unknown>;
		vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.endsWith('/save') ? new Promise((resolve) => { release = resolve; }) : base(input, init)));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		expect(button('Save').disabled).toBe(true);
		expect(button('Save').classList.contains('loading')).toBe(true);
		release({ ok: true, status: 200, json: async () => ({}) });
		await settle();
		expect(dock()!.textContent).toContain('Saved');
	});

	it('words the update offer', async () => {
		offers = [{ ...OFFER, update: true }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.textContent).toContain('Update the saved github.com login?');
		expect(button('Update')).toBeTruthy();
		expect(button('Not now')).toBeTruthy();
		expect(button('Save')).toBeUndefined();
	});

	it('never renders or asks for a password', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.querySelector('input, textarea')).toBeNull();
		expect(dock()!.textContent!.toLowerCase()).not.toContain('password');
	});

	it('announces once in the polite live region, without taking focus', async () => {
		const { socket, canvas } = render();
		canvas.focus();
		const before = document.activeElement;
		await signIn(socket, canvas);
		expect(liveText()).toContain('CORSIA can save this login');
		expect(liveText().split('CORSIA can save this login')).toHaveLength(2);
		expect(document.activeElement).toBe(before);
		expect(dock()!.contains(document.activeElement)).toBe(false);
	});

	it('shortens a long username in the middle and keeps the whole one for hover and screen readers', async () => {
		const long = `${'a'.repeat(30)}@${'b'.repeat(30)}.example.com`;
		offers = [{ ...OFFER, username: long }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		const name = dock()!.querySelector<HTMLElement>('.username')!;
		expect(name.title).toBe(long);
		expect(name.querySelector('[aria-hidden="true"]')!.textContent).toBe(shortenMiddle(long, 32));
		expect(name.querySelector('.sr-only')!.textContent).toBe(long);
	});

	it('shortens a domain only past what two lines hold', () => {
		expect(shortenMiddle('github.com', 48)).toBe('github.com');
		const long = `${'x'.repeat(70)}.example.com`;
		const short = shortenMiddle(long, 48);
		expect(short).toHaveLength(48);
		expect(short).toContain('…');
		expect(short.startsWith('xxx') && short.endsWith('example.com')).toBe(true);
	});
});

describe('save-login offer: Save, Not now and closing', () => {
	it('Save posts, says Saved in the dock and the live region, then closes', async () => {
		vi.useFakeTimers();
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		expect(calls).toContain('POST /api/saved-logins/offer/save {"thread_id":"thread"}');
		expect(dock()!.textContent).toContain('Saved');
		expect(liveText()).toContain('Saved');
		await vi.advanceTimersByTimeAsync(1200);
		flushSync();
		expect(dock()!.classList.contains('fading')).toBe(true);
		await vi.advanceTimersByTimeAsync(250);
		flushSync();
		expect(dock()).toBeNull();
	});

	it('says Updated for an update offer', async () => {
		offers = [{ ...OFFER, update: true }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Update').click();
		await settle();
		expect(dock()!.textContent).toContain('Updated');
		expect(liveText()).toContain('Updated');
	});

	it('closes the dock at once under reduced motion', async () => {
		vi.useFakeTimers();
		reducedMotion.value!.set(true);
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		await vi.advanceTimersByTimeAsync(1200);
		flushSync();
		reducedMotion.value!.set(false);
		expect(dock()).toBeNull();
	});

	it('moves focus to the page before the Save button goes away', async () => {
		vi.useFakeTimers();
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').focus();
		button('Save').click();
		await settle();
		expect(host.querySelector('.browser-page')!.contains(document.activeElement)).toBe(true);
		await vi.advanceTimersByTimeAsync(2000);
		flushSync();
		expect(document.activeElement).not.toBe(document.body);
	});

	it('Not now posts, closes, and leaves focus on the page, not on a removed button', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Not now').focus();
		expect(document.activeElement).toBe(button('Not now'));
		button('Not now').click();
		await settle();
		expect(calls).toContain('POST /api/saved-logins/offer/not-now {"thread_id":"thread"}');
		expect(dock()).toBeNull();
		expect(document.activeElement).not.toBe(document.body);
		expect(host.querySelector('.browser-page')!.contains(document.activeElement)).toBe(true);
	});

	it('closes quietly when the server says the offer is gone', async () => {
		saveStatus = 409;
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		expect(dock()).toBeNull();
		expect(liveText()).not.toContain('Couldn’t save');
	});

	it('shows the error line, announces it, keeps Save and Not now, and allows another try', async () => {
		saveStatus = 500;
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		expect(dock()!.textContent).toContain('Couldn’t save the login. Try again.');
		expect(liveText()).toContain('Couldn’t save the login. Try again.');
		expect(button('Save')).toBeTruthy();
		expect(button('Not now')).toBeTruthy();
		saveStatus = 200;
		button('Save').click();
		await settle();
		expect(dock()!.textContent).toContain('Saved');
	});

	it('closes when the tab moves to another site, and stays for the same site', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		socket.receive({ type: 'navigate_result', url: 'https://github.com/x', title: 'X', domain: 'github.com' });
		await settle();
		expect(dock()).not.toBeNull();
		socket.receive({ type: 'navigate_result', url: 'https://example.org', title: 'Y', domain: 'example.org' });
		await settle();
		expect(dock()).toBeNull();
	});

	it('closes when the pane disconnects', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		socket.close();
		await settle();
		expect(dock()).toBeNull();
	});

	it('keeps the agent viewport still while the dock is up, and sends nothing new after it closes', async () => {
		const { socket, canvas } = render();
		const appearances = () => socket.sent.filter(message => message.type === 'appearance').length;
		const before = appearances();
		await signIn(socket, canvas);
		vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 60));
		socket.receive({ type: 'browser_target_changed' });
		expect(appearances()).toBe(before);
		button('Not now').click();
		await settle();
		expect(dock()).toBeNull();
	});
});
