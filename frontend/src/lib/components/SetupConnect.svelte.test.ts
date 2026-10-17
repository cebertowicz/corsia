import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { SETTINGS_PATH, type PairReason, type PairResult } from '$lib/remoteShell';
import { DISCOVERY_POLL_MS, NONE_FOUND_AFTER_MS, REFUSAL_HOLD_MS } from '$lib/setupRemote.svelte';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
const openExternalUrl = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const flat = (text: string | null | undefined) => (text ?? '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let discovered: { names: string[]; denied?: boolean };
let pairResult: PairResult;
let invoke: ReturnType<typeof vi.fn>;
const onLeaveConnect = vi.fn();

// Every timer in these screens (the poll, the none-found window, the refusal hold, focus after a page change) runs on the fake clock.
async function settle(ms = 0) { await tick(); await vi.advanceTimersByTimeAsync(ms); await tick(); }
const buttons = () => Array.from(host.querySelectorAll('button'));
const button = (label: string) => buttons().find((candidate) => flat(candidate.textContent) === label);
const boxes = () => Array.from(host.querySelectorAll<HTMLInputElement>('input[aria-label^="Code, character"]'));
const paste = (text: string) => boxes()[0].dispatchEvent(Object.assign(new Event('paste', { bubbles: true, cancelable: true }), { clipboardData: { getData: () => text } }));
const sidebar = () => flat(host.querySelector('aside')?.textContent);

async function mountSetup(props: Record<string, unknown> = {}) {
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), ...props } });
	flushSync();
	await settle();
}

async function openConnect() {
	button('Connect to CORSIA on another computer')?.click();
	await settle();
}

async function toCodeScreen(pick = 'Alex’s Mac mini') {
	await openConnect();
	expect(host.textContent).toContain(pick);
	buttons().find((candidate) => candidate.textContent?.includes(pick))?.click();
	await settle();
	button('Continue')?.click();
	await settle();
}

beforeEach(() => {
	vi.useFakeTimers();
	discovered = { names: ['Alex’s Mac mini', 'Living room mini'] };
	pairResult = { ok: true, name: 'Alex’s Mac mini' };
	onLeaveConnect.mockClear(); openExternalUrl.mockClear();
	invoke = vi.fn(async (command: string) => command === 'remote_discovered' ? discovered : command === 'remote_pair' ? pairResult : undefined);
	Element.prototype.scrollIntoView = vi.fn();
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
	(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null; host.remove();
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
	vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('Welcome’s link', () => {
	it('offers Connect to CORSIA on another computer inside the desktop app', async () => {
		await mountSetup();
		expect(button('Connect to CORSIA on another computer')).toBeTruthy();
	});
	it('never shows it in a browser or the phone app, which have no shell', async () => {
		delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
		await mountSetup();
		expect(button('Get started')).toBeTruthy();
		expect(button('Connect to CORSIA on another computer')).toBeUndefined();
	});
});

describe('Choose a computer (r1, r2, r3, r6)', () => {
	it('lists the names found as sidebar rows, with the CONNECT section, and no code beside any', async () => {
		await mountSetup();
		await openConnect();
		expect(host.textContent).toContain('Living room mini');
		expect(sidebar()).toContain('Connect');
		expect(sidebar()).toContain('Choose a computer');
		expect(sidebar()).toContain('Enter the code');
		expect(host.querySelector('h1')?.textContent).toBe('Connect to CORSIA on another computer');
		expect(host.querySelectorAll('ul[aria-label="CORSIA on this network"] li')).toHaveLength(2);
		expect(button('Continue')?.disabled).toBe(true);
	});
	it('polls remote_discovered while open, so a computer that appears later is listed', async () => {
		discovered = { names: [] };
		await mountSetup();
		await openConnect();
		expect(host.textContent).toContain('Looking…');
		discovered = { names: ['Studio'] };
		expect(host.textContent).not.toContain('Studio');
		await settle(DISCOVERY_POLL_MS);
		expect(host.textContent).toContain('Studio');
	});
	it('selects the picked row and enables Continue; typing an address deselects it', async () => {
		await mountSetup();
		await openConnect();
		expect(host.textContent).toContain('Living room mini');
		buttons().find((candidate) => candidate.textContent?.includes('Living room mini'))?.click();
		await settle();
		expect(host.querySelector('ul[aria-label="CORSIA on this network"] .row.current')?.textContent).toContain('Living room mini');
		expect(button('Continue')?.disabled).toBe(false);
		const field = host.querySelector<HTMLInputElement>('#remote-address')!;
		field.value = 'studio.local'; field.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		expect(host.querySelector('ul[aria-label="CORSIA on this network"] .row.current')).toBeNull();
		expect(button('Continue')?.disabled).toBe(false);
	});
	it('says what to check when nothing is found, and keeps looking', async () => {
		discovered = { names: [] };
		await mountSetup();
		await openConnect();
		expect(flat(host.textContent)).not.toContain('No other computer running CORSIA found yet.');
		await settle(NONE_FOUND_AFTER_MS);
		expect(flat(host.textContent)).toContain('No other computer running CORSIA found yet.');
		expect(flat(host.textContent)).toContain(`On that computer: ${flat(SETTINGS_PATH)}`);
		expect(host.textContent).toContain('Looking…');
	});
	it('says macOS blocked the network, with one action that opens the setting', async () => {
		discovered = { names: [], denied: true };
		await mountSetup();
		await openConnect();
		expect(host.textContent).toContain('CORSIA can’t look on this network.');
		button('Open System Settings')?.click();
		expect(openExternalUrl).toHaveBeenCalledWith(expect.stringContaining('Privacy_LocalNetwork'));
	});
	it('Back returns to Welcome', async () => {
		await mountSetup();
		await openConnect();
		button('Back')?.click();
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Welcome to CORSIA');
	});
});

describe('Enter the code (r7)', () => {
	it('shows the lede, the numbered pictures of the other computer, and four empty boxes with the cursor in the first', async () => {
		await mountSetup();
		await toCodeScreen();
		expect(host.querySelector('h1')?.textContent).toBe('Enter the code');
		expect(flat(host.querySelector('.lede')?.textContent)).toBe(`On Alex’s Mac mini: ${flat(SETTINGS_PATH)}, then turn on Let other devices connect. If it’s already on, press New code. Then type the four letters here.`);
		expect(boxes()).toHaveLength(4);
		expect(document.activeElement).toBe(boxes()[0]);
		const steps = host.querySelectorAll('[data-testid="host-steps"] li');
		expect(steps).toHaveLength(3);
		expect(Array.from(steps).map((step) => step.querySelector('.n')?.textContent)).toEqual(['1', '2', '3']);
		// The picture is never one card inside a window frame, and its code is dashes, never letters.
		expect(host.querySelector('.titlebar, .lights')).toBeNull();
		expect(Array.from(host.querySelectorAll<HTMLInputElement>('[data-testid="host-steps"] input')).map((box) => box.value).join('')).toBe('––––');
		expect(host.querySelector('[data-testid="host-steps"]')?.hasAttribute('inert')).toBe(true);
		expect(sidebar()).toContain('Enter the code');
	});
	it('pairs on the fourth character with the picked computer, with no button', async () => {
		await mountSetup();
		await toCodeScreen();
		paste('K7QX');
		await settle();
		expect(invoke).toHaveBeenCalledWith('remote_pair', { target: { kind: 'dns_sd', instance: 'Alex’s Mac mini' }, code: 'K7QX' });
		expect(flat(host.textContent)).toContain('Connecting to Alex’s Mac mini…');
	});
	it('pairs a typed address with its port', async () => {
		await mountSetup();
		await openConnect();
		const field = host.querySelector<HTMLInputElement>('#remote-address')!;
		field.value = '192.168.1.20:47902'; field.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		button('Continue')?.click();
		await settle();
		paste('K7QX');
		await settle();
		expect(invoke).toHaveBeenCalledWith('remote_pair', { target: { kind: 'address', host: '192.168.1.20', port: 47902 }, code: 'K7QX' });
	});

	const onCodeScreen: Array<[PairReason, RegExp]> = [
		['wrong_code', /^That code doesn’t match\. Open CORSIA › Settings › System on Alex’s Mac mini to check it\.$/],
		['expired', /^That code expired\. Open CORSIA › Settings › System on Alex’s Mac mini to get a new one\.$/],
		['too_many_tries', /^Too many wrong codes\. Open CORSIA › Settings › System on Alex’s Mac mini to get a new one\.$/],
		['paused', /^Pairing is paused on Alex’s Mac mini\. Open CORSIA › Settings › System on it and press New code to start again\.$/],
		['busy', /^Alex’s Mac mini is busy with another computer\./],
	];
	it.each(onCodeScreen)('maps %s to its approved line, clears the boxes and puts the cursor back in the first', async (reason, line) => {
		pairResult = { ok: false, reason };
		await mountSetup();
		await toCodeScreen();
		paste('K7QX');
		await settle();
		expect(flat(host.querySelector('p.err')?.textContent)).toMatch(line);
		await settle(REFUSAL_HOLD_MS);
		expect(boxes().map((box) => box.value)).toEqual(['', '', '', '']);
		expect(document.activeElement).toBe(boxes()[0]);
		expect(host.querySelector('h1')?.textContent).toBe('Enter the code');
	});

	const onChooseScreen: Array<[PairReason, string, string]> = [
		['server', '', 'Alex’s Mac mini answered, but CORSIA isn’t running there.'],
		['network', '', 'CORSIA can’t reach Alex’s Mac mini. Check that it’s on and on the same network as this computer.'],
		['incompatible', '1.4/1.2', 'This computer has CORSIA 1.4 and Alex’s Mac mini has 1.2. Update the older one, then try again.'],
		['too_old', '', 'Alex’s Mac mini has an older CORSIA. Update it, then try again.'],
		['local', '', 'This computer couldn’t finish connecting to Alex’s Mac mini.'],
	];
	it.each(onChooseScreen)('maps %s to the choose screen’s line under the address field', async (reason, versions, line) => {
		const [app_version, host_version] = versions ? versions.split('/') : [undefined, undefined];
		pairResult = { ok: false, reason, app_version, host_version };
		await mountSetup();
		await toCodeScreen();
		paste('K7QX');
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Connect to CORSIA on another computer');
		expect(flat(host.querySelector('.field .err')?.textContent)).toContain(line);
	});

	it('Back from the code returns to Choose a computer', async () => {
		await mountSetup();
		await toCodeScreen();
		button('Back')?.click();
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Connect to CORSIA on another computer');
	});

	it('every line on these screens that names Settings reads Open CORSIA › Settings › System', async () => {
		pairResult = { ok: false, reason: 'paused' };
		discovered = { names: [] };
		await mountSetup();
		await openConnect();
		await settle(NONE_FOUND_AFTER_MS);
		expect(flat(host.textContent)).toContain('No other computer running CORSIA found yet.');
		const lines = Array.from(host.querySelectorAll('p')).map((paragraph) => flat(paragraph.textContent)).filter((text) => text.includes('Settings ›'));
		expect(lines.length).toBeGreaterThan(0);
		for (const text of lines) expect(text).toContain('Open CORSIA › Settings › System');
	});
});

describe('opened from Settings', () => {
	it('opens on Choose a computer without checking this computer, and Back leaves', async () => {
		await mountSetup({ startConnect: true, onLeaveConnect });
		expect(host.querySelector('h1')?.textContent).toBe('Connect to CORSIA on another computer');
		expect(fetch).not.toHaveBeenCalledWith('/api/setup/status');
		button('Back')?.click();
		expect(onLeaveConnect).toHaveBeenCalledOnce();
	});
});
