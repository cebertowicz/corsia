import { on } from '$lib/ws/events';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

/**
 * Settings › System › Let other devices connect: the host's own state from `GET /api/devices` (corsia#638), reduced to
 * what the row draws. `code.expires_in_seconds` is the only expiry the server sends; the countdown is counted down
 * here from it and the code is read again when it reaches zero.
 */

export type DevicesApi = {
	enabled: boolean;
	listening: { port?: number; error?: string | null; reason?: string | null };
	announced_name: string | null;
	local_hostname: string;
	/** The person's own name for the host computer (macOS ComputerName), shown while nothing is announced. */
	computer_name: string;
	lan_addresses: string[];
	code: { state: 'none' | 'active' | 'paused'; expires_in_seconds?: number | null; value?: string };
};

export type DevicesView = {
	load: 'loading' | 'ready' | 'failed';
	enabled: boolean;
	/** Seen from a connected computer: no switch, no New code. */
	readOnly: boolean;
	hostName: string;
	announced: boolean;
	busy: null | 'starting';
	listener: 'ok' | 'all-ports-taken' | 'unavailable';
	pairing: 'idle' | 'code' | 'paused';
	code: string | null;
	endsInSeconds: number | null;
	addresses: string[];
};

export const EMPTY_VIEW: DevicesView = {
	load: 'loading', enabled: false, readOnly: false, hostName: '', announced: false, busy: null,
	listener: 'ok', pairing: 'idle', code: null, endsInSeconds: null, addresses: [],
};

/** The host's name as a person knows it: the announced one, else the computer's own name (never the typed `.local` host). */
function nameOf(api: DevicesApi): string {
	return api.announced_name || api.computer_name.replace(/\.local$/i, '');
}

/** Only a taken port is fixed by restarting the computer; any other failure to listen gets its own line. */
function listenerOf(api: DevicesApi): DevicesView['listener'] {
	if (api.enabled || !api.listening?.error) return 'ok';
	return api.listening.reason === 'ports_taken' ? 'all-ports-taken' : 'unavailable';
}

export function viewFromApi(api: DevicesApi, forcedReadOnly: boolean, hostNameOverride?: string): DevicesView {
	// The code's characters come only over direct loopback (G11): an active code without them is a connected computer's view.
	const readOnly = forcedReadOnly || (api.code.state === 'active' && !api.code.value);
	const code = api.code.state === 'active' ? api.code.value ?? null : null;
	return {
		load: 'ready',
		enabled: api.enabled,
		readOnly,
		hostName: hostNameOverride || nameOf(api),
		announced: api.enabled && Boolean(api.announced_name),
		busy: null,
		listener: listenerOf(api),
		pairing: readOnly ? 'idle' : api.code.state === 'active' && code ? 'code' : api.code.state === 'paused' ? 'paused' : 'idle',
		code,
		endsInSeconds: api.code.state === 'active' ? api.code.expires_in_seconds ?? null : null,
		addresses: typedAddresses(api),
	};
}

const DEFAULT_PORT = 47901;

/** What a person types on the other computer: the `.local` name and the LAN addresses, with the port when it isn't the default. */
function typedAddresses(api: DevicesApi): string[] {
	const port = api.listening?.port;
	const suffix = port && port !== DEFAULT_PORT ? `:${port}` : '';
	return [api.local_hostname, ...api.lan_addresses].map((host) => host.includes(':') ? (suffix ? `[${host}]${suffix}` : host) : `${host}${suffix}`);
}

type Options = {
	/** The connected computer's name, when this page is served by another computer. */
	remoteName?: () => string | undefined;
	now?: () => number;
};

export function createDevices(options: Options = {}) {
	const now = options.now ?? Date.now;
	let view = $state<DevicesView>({ ...EMPTY_VIEW });
	let endsAt: number | null = null;
	let remaining = $state<number | null>(null);
	let forcedReadOnly = false;

	const headers = (json = false): Record<string, string> => {
		const current = get(token);
		return { ...(current ? { Authorization: `Bearer ${current}` } : {}), ...(json ? { 'Content-Type': 'application/json' } : {}) };
	};

	function accept(api: DevicesApi) {
		const next = viewFromApi(api, forcedReadOnly || Boolean(options.remoteName?.()), options.remoteName?.());
		endsAt = next.endsInSeconds === null ? null : now() + next.endsInSeconds * 1000;
		remaining = next.endsInSeconds;
		view = { ...next, endsInSeconds: remaining };
	}

	async function read(): Promise<boolean> {
		try {
			const response = await fetch('/api/devices', { headers: headers() });
			if (!response.ok) throw new Error(String(response.status));
			accept(await response.json() as DevicesApi);
			return true;
		} catch {
			if (view.load !== 'ready') view = { ...view, load: 'failed' };
			return false;
		}
	}

	/** Counts the code down locally; the component reads the host again once it reaches 0. */
	function tick() {
		if (endsAt === null) return;
		remaining = Math.max(0, Math.ceil((endsAt - now()) / 1000));
		view = { ...view, endsInSeconds: remaining };
	}

	async function write(path: string, init: RequestInit): Promise<DevicesApi | null> {
		const response = await fetch(path, { ...init, headers: headers(init.body !== undefined) });
		if (response.status === 403) { forcedReadOnly = true; await read(); return null; }
		if (!response.ok) throw new Error(String(response.status));
		return await response.json() as DevicesApi;
	}

	async function setEnabled(next: boolean) {
		const before = view;
		view = { ...view, enabled: next, busy: next ? 'starting' : null, listener: 'ok', pairing: 'idle', code: null, endsInSeconds: null };
		try {
			const api = await write('/api/devices', { method: 'PUT', body: JSON.stringify({ enabled: next }) });
			if (api) accept(api);
		} catch {
			view = { ...before, load: before.load };
			await read();
		}
	}

	async function newCode() {
		try {
			const api = await write('/api/devices/code', { method: 'POST' });
			if (api) accept(api);
		} catch {
			await read();
		}
	}

	async function retry() {
		if (view.load === 'failed') { view = { ...view, load: 'loading' }; await read(); return; }
		await setEnabled(true);
	}

	/**
	 * Reads now, then again on the host's `devices_updated` and whenever the window regains focus or becomes visible. The countdown is local: one timeout aimed at the next moment its wording changes (each whole
	 * minute, then the end).
	 */
	function start() {
		void read();
		let timer: ReturnType<typeof setTimeout> | undefined;
		const arm = () => {
			clearTimeout(timer);
			if (endsAt === null) { timer = setTimeout(arm, 1000); return; }
			const left = Math.max(0, Math.ceil((endsAt - now()) / 1000));
			const next = left > 60 ? 60 * (Math.ceil(left / 60) - 1) : 0;
			timer = setTimeout(() => { tick(); arm(); }, Math.max(250, endsAt - now() - next * 1000 + 50));
		};
		arm();
		const refresh = () => { if (document.visibilityState === 'visible' && view.load === 'ready' && view.busy === null) void read(); };
		// The host says when its state changed (a pairing used the code, it expired, a switch moved); focus is the fallback.
		const offEvent = on('devices_updated', () => { if (view.busy === null) void read(); });
		window.addEventListener('focus', refresh);
		document.addEventListener('visibilitychange', refresh);
		return () => { offEvent(); clearTimeout(timer); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
	}

	return {
		get view() { return view; },
		read, setEnabled, newCode, retry, start, tick,
	};
}
