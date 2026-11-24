import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';
import { shortenMiddle } from '$lib/browserSaveOfferText';
import { CONFIRMATION_HOLD_MS } from '$lib/confirmationHold';
import { theme } from '$lib/stores/theme';

const reducedMotion = vi.hoisted(() => ({ value: null as null | { set(value: boolean): void } }));
vi.mock('$lib/stores/viewport', async (importOriginal) => {
	const { writable: store } = await import('svelte/store');
	const prefersReducedMotion = store(false);
	reducedMotion.value = prefersReducedMotion;
	return { ...(await importOriginal<object>()), prefersReducedMotion };
});


class TestPointerEvent extends MouseEvent {
	pointerId: number;
	pointerType: string;
	isPrimary = true;
	constructor(type: string, init: PointerEventInit = {}) {
		super(type, init);
		this.pointerId = init.pointerId ?? 1;
		this.pointerType = init.pointerType ?? 'touch';
	}
}

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

const OFFER = { site: 'https://example.com', domain: 'example.com', username: 'sam@example.com', update: false };
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let offers: Array<typeof OFFER | null>;
let saveStatus: number;
let saveBody: unknown;
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
	saveBody = { saved: true };
	calls = [];
	document.documentElement.style.setProperty('--dur-standard', '220ms');
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	// jsdom has no Web Animations; a leaving dock only needs one that finishes at once (the motion tests below time it).
	(Element.prototype as unknown as { animate: unknown }).animate = function () {
		const animation = { finished: Promise.resolve(), cancel() {}, onfinish: null as null | (() => void) };
		void Promise.resolve().then(() => animation.onfinish?.());
		return animation;
	};
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('Image', class { src = ''; decode() { return Promise.resolve(); } });
	vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
		calls.push(`${init?.method ?? 'GET'} ${input}${init?.body ? ` ${init.body}` : ''}`);
		if (input.startsWith('/api/saved-logins/offer?')) return { ok: true, status: 200, json: async () => ({ offer: offers.length > 1 ? offers.shift() : offers[0] ?? null }) };
		if (input.endsWith('/save')) return { ok: saveStatus === 200, status: saveStatus, json: async () => saveBody };
		return { ok: true, status: 200, json: async () => ({}) };
	}));
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
	document.documentElement.style.removeProperty('--dur-standard');
	document.documentElement.style.removeProperty('--dur-tight');
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
		expect(dock()!.textContent).toContain('Save the example.com login for agents?');
		expect(dock()!.textContent).toContain('sam@example.com');
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
		socket.receive({ type: 'navigate_result', url: 'https://example.com/home', title: 'Home', domain: 'example.com' });
		await settle();
		expect(gets()).toHaveLength(2);
		expect(dock()).not.toBeNull();
		socket.receive({ type: 'navigate_result', url: 'https://example.com/other', title: 'Other', domain: 'example.com' });
		await settle();
		expect(gets()).toHaveLength(2);
	});

	it('does not ask again on navigate_result after an empty second answer', async () => {
		offers = [null];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		socket.receive({ type: 'navigate_result', url: 'https://example.com/a', title: 'A', domain: 'example.com' });
		await settle();
		socket.receive({ type: 'navigate_result', url: 'https://example.com/b', title: 'B', domain: 'example.com' });
		await settle();
		expect(gets()).toHaveLength(2);
	});
});

describe('save-login offer: a page that navigates by itself', () => {
	const metadata = (socket: FakeSocket, domain: string) => socket.receive({ type: 'browser_metadata', url: `https://${domain}/x`, title: 'T', domain });

	it('closes when browser_metadata names another site, and stays for the same one', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		metadata(socket, 'example.com');
		await settle();
		expect(dock()).not.toBeNull();
		metadata(socket, 'example.org');
		await settle();
		expect(dock()).toBeNull();
	});

	it('stays when the pane names the registrable domain of an offer made at a subdomain', async () => {
		// The pane names a page by its registrable domain; the offer names the full host it saves for.
		offers = [{ ...OFFER, site: 'https://accounts.example.com:8443', domain: 'accounts.example.com:8443' }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		metadata(socket, 'example.com');
		socket.receive({ type: 'navigate_result', url: 'https://example.com/home', title: 'Home', domain: 'example.com' });
		await settle();
		expect(dock()).not.toBeNull();
		metadata(socket, 'notexample.com');
		await settle();
		expect(dock()).toBeNull();
	});

	it('asks the one retry on the next browser_metadata that carries a domain, and not again', async () => {
		offers = [null, OFFER];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(gets()).toHaveLength(1);
		socket.receive({ type: 'browser_metadata', url: 'https://example.com/x', title: 'T' });
		await settle();
		expect(gets()).toHaveLength(1);
		metadata(socket, 'example.com');
		await settle();
		expect(gets()).toHaveLength(2);
		expect(dock()).not.toBeNull();
	});

	it('spends the retry once across browser_metadata and navigate_result together', async () => {
		offers = [null];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		metadata(socket, 'example.com');
		await settle();
		socket.receive({ type: 'navigate_result', url: 'https://example.com/y', title: 'Y', domain: 'example.com' });
		await settle();
		metadata(socket, 'example.com');
		await settle();
		expect(gets()).toHaveLength(2);
	});
});

describe('save-login offer: what it shows', () => {
	it('shows the whole domain, however long', async () => {
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
		// Saving is announced as busy on the button and on the dock, and Not now stays usable.
		expect(button('Save').getAttribute('aria-busy')).toBe('true');
		expect(dock()!.getAttribute('aria-busy')).toBe('true');
		expect(button('Not now').disabled).toBe(false);
		release({ ok: true, status: 200, json: async () => ({}) });
		await settle();
		expect(dock()!.textContent).toContain('Saved');
	});

	it('words the update offer', async () => {
		offers = [{ ...OFFER, update: true }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.textContent).toContain('Update the saved example.com login with this password?');
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
		// What the screen shows: the question with the site, then the username.
		expect(liveText()).toContain('Save the example.com login for agents? sam@example.com');
		expect(liveText().split('sam@example.com')).toHaveLength(2);
		expect(liveText()).not.toContain('CORSIA can save');
		expect(document.activeElement).toBe(before);
		expect(dock()!.contains(document.activeElement)).toBe(false);
	});

	it('names the dock with its question, so a Tab-reached button is announced with it', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		const named = () => host.querySelector(`[id="${dock()!.getAttribute('aria-labelledby')}"]`);
		expect(dock()!.getAttribute('role')).toBe('group');
		expect(named()).toBe(dock()!.querySelector('.question'));
		expect(named()!.textContent).toBe('Save the example.com login for agents?');
	});

	// jsdom has no layout, so the hidden copy of the username "overflows" by its text length: 20 characters fit its line.
	function widthByLength() {
		vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(() => 20);
		vi.spyOn(Element.prototype, 'scrollWidth', 'get').mockImplementation(function (this: Element) { return this.classList.contains('probe') ? (this.textContent ?? '').length : 0; });
	}

	it('shortens a username in the middle only when it overflows its line, keeping the whole one for hover and screen readers', async () => {
		widthByLength();
		const long = `${'a'.repeat(30)}@${'b'.repeat(30)}.example.com`;
		offers = [{ ...OFFER, username: long }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		const name = dock()!.querySelector<HTMLElement>('.username:not(.probe)')!;
		expect(name.title).toBe(long);
		const cut = name.querySelector('[aria-hidden="true"]')!.textContent!;
		expect(cut).toContain('…');
		expect(cut.length).toBeLessThanOrEqual(20);
		expect(cut.startsWith('aaa') && cut.endsWith('.com')).toBe(true);
		expect(name.querySelector('.sr-only')!.textContent).toBe(long);
	});

	it('shows a username that fits its line whole, however many characters it has', async () => {
		widthByLength();
		const fits = 'a-name-of-20-chars!!';
		offers = [{ ...OFFER, username: fits }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.querySelector('.username:not(.probe) [aria-hidden="true"]')!.textContent).toBe(fits);
	});

	it('never cuts a domain: a very long one wraps in full, and only the username is ever shortened', async () => {
		widthByLength();
		const long = `${'x'.repeat(70)}.example.com`;
		offers = [{ ...OFFER, domain: long, username: `${'a'.repeat(30)}@example.com` }];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		const question = dock()!.querySelector('.question')!.textContent!;
		expect(question).toContain(long);
		expect(question).not.toContain('…');
		expect(dock()!.querySelector('.username:not(.probe) [aria-hidden="true"]')!.textContent).toContain('…');
	});

	it('shortenMiddle keeps both ends of a long text and leaves a short one alone', () => {
		expect(shortenMiddle('example.com', 48)).toBe('example.com');
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
		await vi.advanceTimersByTimeAsync(CONFIRMATION_HOLD_MS);
		flushSync();
		expect(dock()!.classList.contains('fading')).toBe(true);
		await vi.advanceTimersByTimeAsync(250);
		flushSync();
		expect(dock()).toBeNull();
	});

	it('holds the Saved line for the app confirmation hold and fades for exactly the motion token', async () => {
		document.documentElement.style.setProperty('--dur-standard', '400ms');
		vi.useFakeTimers();
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		await vi.advanceTimersByTimeAsync(CONFIRMATION_HOLD_MS - 1);
		flushSync();
		expect(dock()!.classList.contains('fading')).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		flushSync();
		expect(dock()!.classList.contains('fading')).toBe(true);
		await vi.advanceTimersByTimeAsync(399);
		flushSync();
		expect(dock()).not.toBeNull();
		await vi.advanceTimersByTimeAsync(1);
		flushSync();
		expect(dock()).toBeNull();
	});

	it('holds the dock at the height it had while it asked, so the Saved line changes no size', async () => {
		// The dock measures itself through the same rect a browser lays out, so the test hands it a real asking height.
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 600, 96));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()!.style.minHeight).toBe('');
		button('Save').click();
		await settle();
		expect(dock()!.style.minHeight).toBe('96px');
		// A floor and not a fixed box: nothing pins the height itself.
		expect(dock()!.style.height).toBe('');
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
		await vi.advanceTimersByTimeAsync(CONFIRMATION_HOLD_MS);
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

	it.each([['a save that works', 200, { saved: true }], ['a save that fails', 500, {}], ['a refused save', 409, { detail: 'Saving isn’t available for this sign-in. Unless the login is already saved, it’s offered again the next time you sign in.' }]])('leaves focus where the person moved it while the save ran: %s', async (_name, status, body) => {
		saveStatus = status;
		saveBody = body;
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		// The person clicked Save, then went on to type in the chat before the answer came back.
		const composer = document.createElement('textarea');
		document.body.append(composer);
		button('Save').click();
		composer.focus();
		await settle();
		await new Promise(resolve => setTimeout(resolve, 0));
		flushSync();
		expect(document.activeElement).toBe(composer);
		composer.remove();
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

	const refuse = async (status: number, body: unknown) => {
		saveStatus = status;
		saveBody = body;
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
	};
	const PANE_LINE = 'Couldn\u2019t save the login. Try again.';

	it.each([[409, 'Saving isn\u2019t available for this sign-in. Unless the login is already saved, it\u2019s offered again the next time you sign in.'], [404, 'Couldn’t find this chat, so no login can be saved for it.']])('a %i refusal puts the server’s sentence in the question’s place and announces it, removes Save, and leaves only Close', async (status, detail) => {
		await refuse(status, { detail });
		expect(dock()!.querySelector('.question')!.textContent).toBe(detail);
		expect(dock()!.textContent).not.toContain('Save the');
		expect(dock()!.querySelector('.error')).toBeNull();
		expect(liveText()).toContain(detail);
		expect(dock()!.textContent).not.toContain('Couldn’t save');
		const names = [...dock()!.querySelectorAll('button')].map(node => node.textContent?.trim());
		expect(names).toEqual(['Close']);
		// The sentence stands alone: no username above Close, and it is still the dock's accessible name.
		expect(dock()!.querySelector('.username')).toBeNull();
		expect(dock()!.textContent).not.toContain('sam@example.com');
		expect(host.querySelector(`[id="${dock()!.getAttribute('aria-labelledby')}"]`)!.textContent).toBe(detail);
	});

	it('keeps the asking height as the dock’s floor when a refusal takes the question’s place', async () => {
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 600, 96));
		await refuse(409, { detail: 'Saving isn’t available for this sign-in. Unless the login is already saved, it’s offered again the next time you sign in.' });
		expect(dock()!.querySelector('.question')!.textContent).toContain('Saving isn’t available for this sign-in');
		expect(dock()!.style.minHeight).toBe('96px');
		expect(dock()!.style.height).toBe('');
	});

	it('moves focus to Close when Save had it, and never fades by itself', async () => {
		vi.useFakeTimers();
		saveStatus = 409;
		saveBody = { detail: 'Saving isn\u2019t available for this sign-in. Unless the login is already saved, it\u2019s offered again the next time you sign in.' };
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').focus();
		button('Save').click();
		await settle();
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(document.activeElement).toBe(button('Close'));
		await vi.advanceTimersByTimeAsync(CONFIRMATION_HOLD_MS * 3);
		flushSync();
		expect(dock()).not.toBeNull();
		expect(dock()!.classList.contains('fading')).toBe(false);
	});

	it('Close dismisses as Not now does: it posts not-now, closes, and leaves focus on the page', async () => {
		await refuse(409, { detail: 'Saving isn\u2019t available for this sign-in. Unless the login is already saved, it\u2019s offered again the next time you sign in.' });
		button('Close').focus();
		button('Close').click();
		await settle();
		expect(calls).toContain('POST /api/saved-logins/offer/not-now {"thread_id":"thread"}');
		expect(dock()).toBeNull();
		expect(host.querySelector('.browser-page')!.contains(document.activeElement)).toBe(true);
	});

	it.each([
		[400, { detail: 'Couldn’t do that: the request didn’t name a valid chat. Send it again with the chat’s id.' }],
		[401, { detail: 'Sign in again to continue.' }],
		[403, { detail: 'Only the person can save a login.' }],
		[422, { detail: [{ msg: 'field required' }] }],
		[500, { detail: 'internal' }],
		[503, {}],
	])('shows the pane’s own line for a %i, whatever the body says, and keeps Save and Not now', async (status, body) => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		await refuse(status, body);
		expect(dock()!.querySelector('.error')!.textContent).toBe(PANE_LINE);
		expect(liveText()).toContain(PANE_LINE);
		expect(dock()!.querySelector('.question')!.textContent).toContain('Save the example.com login');
		expect(button('Save')).toBeTruthy();
		expect(button('Not now')).toBeTruthy();
		expect(button('Close')).toBeUndefined();
	});

	it('shows the pane\u2019s own line when the save request throws', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const base = fetch as unknown as (input: string, init?: RequestInit) => Promise<unknown>;
		vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => { if (input.endsWith('/save')) throw new Error('offline'); return base(input, init); }));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		expect(dock()!.querySelector('.error')!.textContent).toBe(PANE_LINE);
	});

	it('shows the pane\u2019s own line for a refusal whose body has no text', async () => {
		await refuse(409, 'gone');
		expect(dock()!.querySelector('.error')!.textContent).toBe(PANE_LINE);
		expect(button('Save')).toBeTruthy();
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
		socket.receive({ type: 'navigate_result', url: 'https://example.com/x', title: 'X', domain: 'example.com' });
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

	it('lies over the bottom edge of the page, so the canvas keeps its size and a new target, density and theme go out as they are', async () => {
		const { socket, canvas } = render();
		const appearances = () => socket.sent.filter(message => message.type === 'appearance');
		await signIn(socket, canvas);
		expect(dock()!.parentElement!.classList.contains('browser-page')).toBe(true);
		expect(dock()!.parentElement!.contains(canvas)).toBe(true);
		const before = appearances().length;
		socket.receive({ type: 'browser_target_changed' });
		expect(appearances()).toHaveLength(before + 1);
		expect(appearances().at(-1)).toEqual({ type: 'appearance', width: 100, height: 100, scale: 1, scheme: 'dark' });
		vi.stubGlobal('devicePixelRatio', 2);
		theme.set('light');
		await new Promise(resolve => setTimeout(resolve, 0));
		expect(appearances().at(-1)).toEqual({ type: 'appearance', width: 100, height: 100, scale: 2, scheme: 'light' });
		button('Not now').click();
		await settle();
		theme.set('dark');
	});
});

describe('save-login offer: touch typing and failures', () => {
	// A tap on a password field opens the touch overlay's own input; what it types goes to the server through the leaf's send.
	async function touchTyping(socket: FakeSocket, canvas: HTMLCanvasElement, text: string) {
		Object.defineProperties(HTMLCanvasElement.prototype, {
			setPointerCapture: { configurable: true, value() {} },
			releasePointerCapture: { configurable: true, value() {} },
			hasPointerCapture: { configurable: true, value: () => true },
		});
		await settle();
		socket.receive({ type: 'text_fields', fields: [{ x: 10, y: 5, width: 40, height: 20, kind: 'password' }] });
		for (const type of ['pointerdown', 'pointerup']) canvas.dispatchEvent(new TestPointerEvent(type, { bubbles: true, cancelable: true, clientX: 20, clientY: 35, pointerType: 'touch' }));
		flushSync();
		const input = host.querySelector<HTMLInputElement>('input[data-pane-field]')!;
		input.value = text;
		input.dispatchEvent(new Event('input', { bubbles: true }));
	}

	it.each([['one character', 'x'], ['a pasted run', 'xyz']])('counts the touch overlay\'s typing (%s) as the person typing, and asks once', async (_name, text) => {
		const { socket, canvas } = render();
		passwordPage(socket, true);
		await touchTyping(socket, canvas, text);
		passwordPage(socket, false);
		await settle();
		expect(gets()).toEqual(['GET /api/saved-logins/offer?thread_id=thread']);
		expect(dock()!.textContent).toContain('Save the example.com login for agents?');
	});

	it('does not count a tap on the overlay when nothing was typed', async () => {
		const { socket, canvas } = render();
		passwordPage(socket, true);
		await touchTyping(socket, canvas, '');
		passwordPage(socket, false);
		await settle();
		expect(gets()).toEqual([]);
	});

	it('reports a refused read to the console and shows nothing', async () => {
		const report = vi.spyOn(console, 'error').mockImplementation(() => {});
		const base = fetch as unknown as (input: string, init?: RequestInit) => Promise<unknown>;
		vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => input.startsWith('/api/saved-logins/offer?') ? { ok: false, status: 500, json: async () => ({}) } : base(input, init)));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()).toBeNull();
		expect(report).toHaveBeenCalledWith('[saved login offer] read rejected', 'thread', 500);
	});

	it('reports a read that throws to the console and shows nothing', async () => {
		const report = vi.spyOn(console, 'error').mockImplementation(() => {});
		const failure = new Error('offline');
		vi.stubGlobal('fetch', vi.fn(async () => { throw failure; }));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()).toBeNull();
		expect(report).toHaveBeenCalledWith('[saved login offer] read failed', 'thread', failure);
	});

	it('does not report a 200 with no offer', async () => {
		const report = vi.spyOn(console, 'error').mockImplementation(() => {});
		offers = [null];
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(dock()).toBeNull();
		expect(report).not.toHaveBeenCalled();
	});

	it('closes on Not now even when the server refuses it, and reports the refusal', async () => {
		const report = vi.spyOn(console, 'error').mockImplementation(() => {});
		const base = fetch as unknown as (input: string, init?: RequestInit) => Promise<unknown>;
		vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => input.endsWith('/not-now') ? { ok: false, status: 503, json: async () => ({}) } : base(input, init)));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Not now').click();
		await settle();
		expect(dock()).toBeNull();
		expect(report).toHaveBeenCalledWith('[saved login offer] not-now rejected', 'thread', 503);
	});

	it('closes on Not now when the request throws, and reports it', async () => {
		const report = vi.spyOn(console, 'error').mockImplementation(() => {});
		const base = fetch as unknown as (input: string, init?: RequestInit) => Promise<unknown>;
		const failure = new Error('offline');
		vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => { if (input.endsWith('/not-now')) throw failure; return base(input, init); }));
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Not now').click();
		await settle();
		expect(dock()).toBeNull();
		expect(report).toHaveBeenCalledWith('[saved login offer] not-now failed', 'thread', failure);
	});
});

describe('save-login offer: motion', () => {
	type Played = { node: Element; keyframes: Keyframe[]; options: KeyframeAnimationOptions };
	let played: Played[];
	const forDock = () => played.filter(entry => entry.node.classList.contains('save-offer'));
	const forError = () => played.filter(entry => entry.node.classList.contains('error'));
	const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

	beforeEach(() => {
		played = [];
		const root = document.documentElement.style;
		root.setProperty('--ease-snap', 'cubic-bezier(0.2, 0, 0, 1)');
		root.setProperty('--ease-entrance', 'cubic-bezier(0.22, 1, 0.36, 1)');
		// jsdom has no Web Animations: record what the dock and the error line ask for.
		(Element.prototype as unknown as { animate: unknown }).animate = function (this: Element, keyframes: Keyframe[], options: KeyframeAnimationOptions) {
			// Svelte books each transition with an empty animation of its own; only the ones the dock asked for are kept.
			if (keyframes.length) played.push({ node: this, keyframes, options });
			return {
				finished: Promise.resolve(),
				cancel() {},
				set onfinish(done: () => void) { setTimeout(done, Number(options.duration)); },
			};
		};
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 64));
	});

	afterEach(() => {
		const root = document.documentElement.style;
		['--ease-snap', '--ease-entrance'].forEach(name => root.removeProperty(name));
	});

	it('fades the dock out on exit at 0.7 of the standard duration', async () => {
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		expect(forDock()).toEqual([]);
		button('Not now').click();
		await settle();
		await wait(20);
		const exit = forDock().at(-1)!;
		expect(exit.keyframes.at(-1)).toMatchObject({ opacity: '0' });
		expect(exit.keyframes.every(frame => !('height' in frame))).toBe(true);
		expect(exit.options.duration).toBeCloseTo(154);
		expect(dock()).not.toBeNull();
		await wait(300);
		expect(dock()).toBeNull();
	});

	it('after Saved, the exit starts from the dock’s real opacity of 0 and never rises', async () => {
		vi.useFakeTimers();
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		// jsdom runs no stylesheet: the finished fade is stood in for by the opacity it would have reached.
		dock()!.style.opacity = '0';
		await vi.advanceTimersByTimeAsync(CONFIRMATION_HOLD_MS + 230);
		flushSync();
		const exit = forDock().at(-1)!;
		expect(exit.keyframes.length).toBeGreaterThan(0);
		expect(exit.keyframes.every(frame => Number(frame.opacity) === 0)).toBe(true);
	});

	it('animates the error line in the same way', async () => {
		saveStatus = 500;
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Save').click();
		await settle();
		const [arrival] = forError();
		expect(arrival.keyframes[0]).toMatchObject({ height: '0px', opacity: 0 });
		expect(arrival.options.duration).toBe(220);
		saveStatus = 200;
		button('Save').click();
		await settle();
		expect(forError().at(-1)!.keyframes.at(-1)).toMatchObject({ height: '0px', opacity: 0 });
	});

	// The dock is 64 px while it asks; the closed-browser sentence needs 84.
	const CLOSED = 'The shared browser this sign-in came from has closed, so it can’t be saved. It’s offered again the next time you sign in.';
	const refuseWith = async (detail: string, refusedHeight: number) => {
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			const refused = this.classList.contains('save-offer') && this.querySelector('.question')?.textContent === detail;
			return new DOMRect(0, 0, 100, refused ? refusedHeight : 64);
		});
		saveStatus = 409;
		saveBody = { detail };
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		played = [];
		button('Save').click();
		await settle();
		await wait(0);
	};
	const sized = () => forDock().filter(entry => entry.keyframes.some(frame => 'height' in frame));

	it('eases the extra height in over the standard duration when a refusal needs more room', async () => {
		await refuseWith(CLOSED, 84);
		const [grow] = sized();
		expect(grow.keyframes).toEqual([{ height: '64px' }, { height: '84px' }]);
		expect(grow.options).toMatchObject({ duration: 220, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
		expect(dock()!.style.minHeight).toBe('64px');
		// The clip lasts only as long as the ease.
		expect(dock()!.style.overflow).toBe('');
	});

	it('plays no size change for a refusal that fits the asking height', async () => {
		await refuseWith(CLOSED, 64);
		expect(sized()).toEqual([]);
		expect(played.filter(entry => entry.node.classList.contains('body'))).toEqual([]);
	});

	it('crossfades the content instead of easing the height under reduced motion', async () => {
		reducedMotion.value!.set(true);
		await refuseWith(CLOSED, 84);
		reducedMotion.value!.set(false);
		expect(sized()).toEqual([]);
		const [fade] = played.filter(entry => entry.node.classList.contains('body'));
		expect(fade.keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }]);
		expect(fade.options.duration).toBe(120);
	});

	it('crossfades over the tight duration under reduced motion', async () => {
		reducedMotion.value!.set(true);
		const { socket, canvas } = render();
		await signIn(socket, canvas);
		button('Not now').click();
		await settle();
		await wait(20);
		const exit = forDock().at(-1)!;
		expect(exit.options.duration).toBeCloseTo(84);
		expect(exit.keyframes.every(frame => Object.keys(frame).every(key => key === 'opacity' || key === 'offset'))).toBe(true);
		reducedMotion.value!.set(false);
	});
});
