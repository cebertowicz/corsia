import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

/**
 * Settings › System › Let other devices connect: the host's own state from `GET /api/devices` (corsia#638), reduced to
 * what the row draws. `code.expires_in_seconds` is the only expiry the server sends; the countdown is counted down
 * here from it and the code is read again when it reaches zero.
 */

export type DevicesApi = {
	enabled: boolean;
	listening: { port?: number; error?: string | null };
	announced_name: string | null;
	local_hostname: string;
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
	listener: 'ok' | 'all-ports-taken';
	pairing: 'idle' | 'code' | 'paused';
	code: string | null;
	endsInSeconds: number | null;
	addresses: string[];
};

export const EMPTY_VIEW: DevicesView = {
	load: 'loading', enabled: false, readOnly: false, hostName: '', announced: false, busy: null,
	listener: 'ok', pairing: 'idle', code: null, endsInSeconds: null, addresses: [],
};

/** The host's name as a person knows it: the announced one, else the machine's name without `.local`. */
function nameOf(api: DevicesApi): string {
	return api.announced_name || api.local_hostname.replace(/\.local$/i, '');
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
		listener: !api.enabled && Boolean(api.listening?.error) ? 'all-ports-taken' : 'ok',
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
	let timer: ReturnType<typeof setInterval> | undefined;
	let refetching = false;

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

	function tick() {
		if (endsAt === null) return;
		remaining = Math.max(0, Math.ceil((endsAt - now()) / 1000));
		view = { ...view, endsInSeconds: remaining };
		if (remaining === 0 && !refetching) {
			refetching = true;
			void read().finally(() => { refetching = false; });
		}
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

	function start(pollMs = 5000) {
		void read();
		timer = setInterval(() => { tick(); }, 1000);
		const poll = setInterval(() => { if (view.load === 'ready' && view.busy === null) void read(); }, pollMs);
		return () => { if (timer) clearInterval(timer); clearInterval(poll); };
	}

	return {
		get view() { return view; },
		read, setEnabled, newCode, retry, start, tick,
	};
}
