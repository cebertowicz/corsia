import { apiFetch } from '$lib/api/request';

/**
 * Settings › System › Urgent notifications: this device's state, read from the browser's own subscription (not only
 * the server row), plus the person's other devices from `GET /api/push` (each with `added_at`, an ISO time). The OS
 * permission prompt appears only inside `enable()`, which a tap calls; nothing here prompts on load.
 */

export const PUSH_WORKER = '/push-sw.js';
/** The key of this device's row in `busy` and `problems`; other rows use the server's device id. */
export const THIS_DEVICE = 'this';

export type ThisDevice =
	| 'loading'
	| 'off'
	| 'enabling'
	| 'on'
	| 'denied'
	| 'turned-off'
	| 'needs-home-screen'
	| 'unsupported';

export type ListedDevice = { id: string; name: string; state: 'on' | 'turned_off'; endpoint_id: string; added_at?: string };

/** The company whose service delivers a notification to a device: the one that sees when and to which device. */
export type Company = 'Apple' | 'Google' | 'Mozilla' | 'Microsoft';
export type Busy = 'turning-off' | 'removing';

export type PushView = {
	thisDevice: ThisDevice;
	platform: 'ios' | 'other';
	inDesktopApp: boolean;
	company: Company | null;
	others: ListedDevice[];
	/** Rows with a change in flight. */
	busy: Record<string, Busy>;
	/** Rows whose last change failed: the sentence the row shows. */
	problems: Record<string, string>;
};

type Api = { public_key: string; devices: ListedDevice[] };

const SERVICES: Array<[string, Company]> = [
	['push.apple.com', 'Apple'],
	['fcm.googleapis.com', 'Google'],
	['push.services.mozilla.com', 'Mozilla'],
	['notify.windows.com', 'Microsoft'],
];

/** The company behind a subscription's endpoint, or null for a host that is none of the four. */
export function companyOf(endpoint: string): Company | null {
	let host: string;
	try { host = new URL(endpoint).hostname; } catch { return null; }
	return SERVICES.find(([suffix]) => host === suffix || host.endsWith(`.${suffix}`))?.[1] ?? null;
}

const isIos = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const isStandalone = () =>
	(navigator as unknown as { standalone?: boolean }).standalone === true || matchMedia('(display-mode: standalone)').matches;

async function sha256(text: string): Promise<string> {
	const bytes = new TextEncoder().encode(text);
	return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function key(base64: string): Uint8Array {
	const padded = base64.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(base64.length / 4) * 4, '=');
	return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

function deviceName(): string {
	const ua = navigator.userAgent;
	const where = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : 'Linux';
	const how = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : 'Safari';
	return where === 'iPhone' || where === 'iPad' ? where : `${how} on ${where}`;
}

const without = <T>(record: Record<string, T>, id: string): Record<string, T> => {
	const { [id]: _gone, ...rest } = record;
	return rest;
};

export function createPush() {
	let view = $state<PushView>({
		thisDevice: 'loading', platform: 'other', inDesktopApp: false, company: null, others: [], busy: {}, problems: {},
	});
	let server: Api | null = null;
	let reading = false;
	/** Each tap on the switch is an attempt; Cancel ends the one in flight, and its late answer is ignored. */
	let attempt = 0;

	const supported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
	const setBusy = (id: string, busy: Busy) => { view = { ...view, busy: { ...view.busy, [id]: busy }, problems: without(view.problems, id) }; };
	const setProblem = (id: string, text: string) => { view = { ...view, busy: without(view.busy, id), problems: { ...view.problems, [id]: text } }; };
	const clearBusy = (id: string) => { view = { ...view, busy: without(view.busy, id) }; };

	async function localSubscription(): Promise<PushSubscription | null> {
		const registration = await navigator.serviceWorker.getRegistration(PUSH_WORKER);
		return registration ? registration.pushManager.getSubscription() : null;
	}

	/** Reads the browser and the server. Rows with a change in flight, and failures already shown, are kept. */
	async function read() {
		reading = true;
		try {
			const platform = isIos() ? 'ios' : 'other';
			const inDesktopApp = typeof (window as unknown as Record<string, unknown>).__CORSIA_SHELL__ !== 'undefined' && !('PushManager' in window);
			let thisDevice: ThisDevice;
			if (platform === 'ios' && !isStandalone()) thisDevice = 'needs-home-screen';
			else if (!supported()) thisDevice = 'unsupported';
			else if (Notification.permission === 'denied') thisDevice = 'denied';
			else thisDevice = 'off';
			// Named only from this device's own subscription; with none there is nothing to name, and nothing is guessed.
			let company: Company | null = null;
			let others: ListedDevice[] = [];
			try {
				const response = await apiFetch('/api/push');
				if (!response.ok) throw new Error(String(response.status));
				server = await response.json() as Api;
				others = server.devices;
			} catch {
				server = null;
			}
			if (thisDevice === 'off') {
				// On only when this browser holds a subscription and the server has its row on; the server row alone is not enough.
				const sub = await localSubscription();
				const mine = sub ? await sha256(sub.endpoint) : null;
				const row = mine ? others.find((d) => d.endpoint_id === mine) : undefined;
				if (sub) company = companyOf(sub.endpoint);
				if (row) {
					thisDevice = row.state === 'on' ? 'on' : 'turned-off';
					others = others.filter((d) => d !== row);
				}
			}
			view = { thisDevice, platform, inDesktopApp, company, others, busy: view.busy, problems: view.problems };
		} finally {
			reading = false;
		}
	}

	/** Reads again when the person comes back to the page, so a device added or turned off meanwhile shows. */
	function watch(): () => void {
		const refresh = () => {
			if (document.visibilityState === 'hidden' || reading) return;
			if (view.thisDevice === 'enabling' || Object.keys(view.busy).length > 0) return;
			void read();
		};
		document.addEventListener('visibilitychange', refresh);
		window.addEventListener('focus', refresh);
		return () => {
			document.removeEventListener('visibilitychange', refresh);
			window.removeEventListener('focus', refresh);
		};
	}

	/** A tap on the switch. The permission prompt is asked here and nowhere else. */
	async function enable() {
		if (!server) await read();
		if (!server) { setProblem(THIS_DEVICE, 'Couldn’t reach CORSIA. Try again.'); return; }
		const mine = ++attempt;
		view = { ...view, thisDevice: 'enabling', problems: without(view.problems, THIS_DEVICE) };
		try {
			const permission = await Notification.requestPermission();
			if (mine !== attempt) return;
			if (permission !== 'granted') { view = { ...view, thisDevice: permission === 'denied' ? 'denied' : 'off' }; return; }
			const registration = await navigator.serviceWorker.register(PUSH_WORKER);
			await navigator.serviceWorker.ready;
			const sub = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key(server.public_key) as BufferSource });
			if (mine !== attempt) { await sub.unsubscribe(); return; }
			const response = await apiFetch('/api/push/devices', {
				method: 'PUT', headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ subscription: sub.toJSON(), name: deviceName() }),
			});
			if (!response.ok) throw new Error(String(response.status));
			await read();
		} catch {
			if (mine !== attempt) return;
			await read();
			setProblem(THIS_DEVICE, 'Couldn’t turn on urgent notifications on this device. Try again.');
		}
	}

	/** The way out of a prompt the person ignores: the switch goes back to Off and a late answer does nothing. */
	function cancel() {
		attempt++;
		view = { ...view, thisDevice: Notification.permission === 'denied' ? 'denied' : 'off' };
	}

	async function disable() {
		setBusy(THIS_DEVICE, 'turning-off');
		try {
			const sub = await localSubscription();
			const mine = sub ? await sha256(sub.endpoint) : null;
			const row = mine ? (await listAll()).find((d) => d.endpoint_id === mine) : undefined;
			if (row) {
				const response = await apiFetch(`/api/push/devices/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
				if (!response.ok) throw new Error(String(response.status));
			}
			await sub?.unsubscribe();
			clearBusy(THIS_DEVICE);
			await read();
		} catch {
			setProblem(THIS_DEVICE, 'Couldn’t turn off urgent notifications on this device. Try again.');
		}
	}

	async function listAll(): Promise<ListedDevice[]> {
		const response = await apiFetch('/api/push');
		if (!response.ok) throw new Error(String(response.status));
		return ((await response.json()) as Api).devices;
	}

	/** Turn off (a device that is on) or Remove (one that was turned off), for a device other than this one. */
	async function remove(id: string) {
		const device = view.others.find((d) => d.id === id);
		const turningOff = device?.state === 'on';
		setBusy(id, turningOff ? 'turning-off' : 'removing');
		try {
			const response = await apiFetch(`/api/push/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
			if (!response.ok) throw new Error(String(response.status));
			clearBusy(id);
			await read();
		} catch {
			setProblem(id, turningOff ? `Couldn’t turn off urgent notifications on ${device?.name ?? 'that device'}. Try again.` : `Couldn’t remove ${device?.name ?? 'that device'}. Try again.`);
		}
	}

	return { get view() { return view; }, read, watch, enable, cancel, disable, remove };
}
