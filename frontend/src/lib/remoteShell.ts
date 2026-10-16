/**
 * The page's side of the desktop shell's remote-connect contract (corsia#638). The shell injects
 * `window.__CORSIA_SHELL__` on every page load and registers the commands below; a browser and the phone app have
 * neither, so every remote-connect surface checks `shellInfo()` first.
 */

export type ShellInfo = { mode: 'local' } | { mode: 'remote'; name: string };

export type PairTarget =
	| { kind: 'dns_sd'; instance: string }
	| { kind: 'address'; host: string; port?: number };

export type PairReason =
	| 'wrong_code' | 'expired' | 'too_many_tries' | 'paused' | 'busy'
	| 'network' | 'server' | 'incompatible' | 'too_old';

export type PairResult =
	| { ok: true; name: string }
	| { ok: false; reason: PairReason; app_version?: string; host_version?: string };

export type DiscoveredResult = { names: string[]; denied?: boolean };

type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;
type ShellWindow = Window & { __CORSIA_SHELL__?: ShellInfo; __TAURI_INTERNALS__?: { invoke?: Invoke } };

export function shellInfo(): ShellInfo | null {
	if (typeof window === 'undefined') return null;
	const info = (window as ShellWindow).__CORSIA_SHELL__;
	if (!info) return null;
	return info.mode === 'remote' && typeof info.name === 'string' ? info : { mode: 'local' };
}

function invoke(command: string, args?: Record<string, unknown>): Promise<unknown> {
	const call = (window as ShellWindow).__TAURI_INTERNALS__?.invoke;
	if (!call) return Promise.reject(new Error('shell unavailable'));
	return args === undefined ? call(command) : call(command, args);
}

export async function remoteDiscovered(): Promise<DiscoveredResult> {
	const result = await invoke('remote_discovered') as DiscoveredResult | null;
	return { names: Array.isArray(result?.names) ? result.names : [], denied: result?.denied === true };
}

const REASONS: readonly PairReason[] = ['wrong_code', 'expired', 'too_many_tries', 'paused', 'busy', 'network', 'server', 'incompatible', 'too_old'];

export async function remotePair(target: PairTarget, code: string): Promise<PairResult> {
	try {
		const result = await invoke('remote_pair', { target, code }) as PairResult | null;
		if (result?.ok === true) return { ok: true, name: result.name };
		if (result && result.ok === false && REASONS.includes(result.reason)) return result;
	} catch {
		// The shell refused or is gone: say the server could not be reached, never a wrong code.
	}
	return { ok: false, reason: 'network' };
}

export async function disconnectRemote(): Promise<void> {
	await invoke('disconnect_remote');
}

/** The typed address as the shell takes it: host, and the port when `host:port` was typed. */
export function parseAddress(text: string): { host: string; port?: number } | null {
	const value = text.trim();
	if (!value || /\s/.test(value)) return null;
	const bracketed = /^\[([^\]]+)\](?::(\d{1,5}))?$/.exec(value);
	if (bracketed) return withPort(bracketed[1], bracketed[2]);
	if ((value.match(/:/g) ?? []).length > 1) return { host: value };
	const [host, port] = value.split(':');
	return host ? withPort(host, port) : null;
}

function withPort(host: string, port: string | undefined): { host: string; port?: number } | null {
	if (port === undefined) return { host };
	const number = Number(port);
	return number >= 1 && number <= 65535 ? { host, port: number } : null;
}

// "Open CORSIA › Settings › System" never splits across lines: no-break spaces around the chevrons.
const NB = ' ';
export const SETTINGS_PATH = `Open${NB}CORSIA${NB}›${NB}Settings${NB}›${NB}System`;

export type RefusalContext = { name: string; appVersion?: string; hostVersion?: string };

/** The line under the code boxes when a code is refused; null for causes that belong to the choose screen. */
export function codeRefusalLine(reason: PairReason, name: string): string | null {
	switch (reason) {
		case 'wrong_code': return `That code doesn’t match. ${SETTINGS_PATH} on ${name} to check it.`;
		case 'expired': return `That code expired. ${SETTINGS_PATH} on ${name} to get a new one.`;
		case 'too_many_tries': return `Too many wrong codes. ${SETTINGS_PATH} on ${name} to get a new one.`;
		case 'paused': return `Pairing is paused on ${name}. ${SETTINGS_PATH} on it and press New code to start again.`;
		case 'busy': return `${name} is busy with another computer. Try again in a moment.`;
		default: return null;
	}
}

/** The line under the address field when the computer can't be used (r4, r5 and the too-old host). */
export function unreachableLine(reason: PairReason, target: string, versions: { app?: string; host?: string }): string | null {
	switch (reason) {
		// Setup's approved r4 frame draws one line for "can't reach it"; no separate network wording is approved.
		case 'network':
		case 'server': return `${target} answered, but CORSIA isn’t running there.`;
		case 'incompatible': return `This computer has CORSIA ${versions.app ?? 'a different version'} and ${target} has ${versions.host ?? 'another'}. Update the older one, then try again.`;
		case 'too_old': return `${target} has an older CORSIA. Update it, then try again.`;
		default: return null;
	}
}

/** Causes that end on the code screen (the person fixes the code) as against the choose screen (the person fixes the target). */
export function refusesOnCodeScreen(reason: PairReason): boolean {
	return codeRefusalLine(reason, '') !== null;
}

export const SYSTEM_SETTINGS_LOCAL_NETWORK = 'x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork';

/** Moves CORSIA's work folder before the account is created (the desktop shell only; a browser has no folder to move). */
export async function applyWorkFolder(path: string): Promise<void> {
	const invoke = (window as Window & { __TAURI_INTERNALS__?: { invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;
	if (invoke) await invoke('configure_work_folder', { path });
}
