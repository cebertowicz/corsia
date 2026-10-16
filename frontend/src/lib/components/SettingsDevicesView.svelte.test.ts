import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsDevicesView from './SettingsDevicesView.svelte';
import { EMPTY_VIEW, type DevicesView } from '$lib/devices.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
const flat = (text: string | null | undefined) => (text ?? '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const base: DevicesView = { ...EMPTY_VIEW, load: 'ready', hostName: 'Studio', addresses: ['studio.local', '192.168.1.20'] };

function show(view: Partial<DevicesView>, handlers: Record<string, () => void> = {}) {
	component = mount(SettingsDevicesView, { target: host, props: { view: { ...base, ...view }, ...handlers } });
	flushSync();
	return flat(host.textContent);
}
const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label="Let other devices connect"]');

beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); });
afterEach(() => { if (component) void unmount(component); component = null; host.remove(); });

describe('SettingsDevicesView', () => {
	it('loading: dims the switch and says Loading…', () => {
		const text = show({ load: 'loading' });
		expect(text).toContain('Loading…');
		expect(toggle()?.disabled).toBe(true);
	});
	it('off: the switch is off and nothing else shows', () => {
		const text = show({ enabled: false });
		expect(text).toContain('Let other devices connect');
		expect(toggle()?.getAttribute('aria-pressed')).toBe('false');
		expect(text).not.toContain('New code');
		expect(text).not.toContain('Visible as');
	});
	it('on and visible: names the computer and offers the code with its countdown', () => {
		const text = show({ enabled: true, announced: true, pairing: 'code', code: 'K7QX', endsInSeconds: 540 });
		expect(text).toContain('Visible as Studio');
		expect(text).toContain('Ends in 9 min');
		expect(text).toContain('New code');
		expect(Array.from(host.querySelectorAll<HTMLInputElement>('input')).map((box) => box.value).join('')).toBe('K7QX');
	});
	it('the last minute reads Ends in under a minute', () => {
		expect(show({ enabled: true, announced: true, pairing: 'code', code: 'K7QX', endsInSeconds: 42 })).toContain('Ends in under a minute');
	});
	it('on but not visible: lists what to type, IPv4 before IPv6', () => {
		const text = show({ enabled: true, announced: false, addresses: ['studio.local', '192.168.1.20', '[fd5c::1]'] });
		expect(text).toContain('This computer isn’t visible on the network. Other computers can still connect by typing studio.local or 192.168.1.20.');
		expect(text).not.toContain('fd5c');
	});
	it('paused: says so and keeps New code', () => {
		const text = show({ enabled: true, announced: true, pairing: 'paused' });
		expect(text).toContain('Pairing is paused after too many wrong codes.');
		expect(text).toContain('New code');
	});
	it('starting: locks the switch on and shows Starting…', () => {
		const text = show({ enabled: true, busy: 'starting' });
		expect(text).toContain('Starting…');
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true');
		expect(text).not.toContain('New code');
	});
	it('every port taken: off with the restart line and Try again', () => {
		const onRetry = vi.fn();
		const text = show({ enabled: false, listener: 'all-ports-taken' }, { onRetry });
		expect(text).toContain('Couldn’t turn this on. Other apps are using the connections CORSIA needs. Restart this computer, then try again.');
		Array.from(host.querySelectorAll('button')).find((b) => flat(b.textContent) === 'Try again')?.click();
		expect(onRetry).toHaveBeenCalledOnce();
	});
	it('failed read: Couldn’t load and no switch', () => {
		const text = show({ load: 'failed' });
		expect(text).toContain('Couldn’t load device settings.');
		expect(toggle()).toBeNull();
	});
	it('read-only (seen from a connected computer): no switch, no New code, and where to manage it', () => {
		const text = show({ readOnly: true, enabled: true, announced: true, hostName: 'Alex’s Mac mini' });
		expect(text).toContain('Manage connections on Alex’s Mac mini itself.');
		expect(toggle()).toBeNull();
		expect(text).not.toContain('New code');
	});
	it('the switch and New code call their handlers', () => {
		const onToggle = vi.fn(); const onNewCode = vi.fn();
		show({ enabled: true, announced: true, pairing: 'code', code: 'K7QX', endsInSeconds: 300 }, { onToggle, onNewCode } as never);
		toggle()?.click();
		expect(onToggle).toHaveBeenCalledWith(false);
		Array.from(host.querySelectorAll('button')).find((b) => flat(b.textContent) === 'New code')?.click();
		expect(onNewCode).toHaveBeenCalledOnce();
	});
});
