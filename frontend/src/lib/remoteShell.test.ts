import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	SETTINGS_PATH, codeRefusalLine, disconnectRemote, parseAddress, onStartupShellPage, recoverLocalService, refusesOnCodeScreen, remoteDiscovered, remotePair, shellInfo,
	unreachableLine, type PairReason,
} from './remoteShell';
import pairRequests from '../../../testdata/remote-pair-requests.json';

afterEach(() => {
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
	vi.unstubAllGlobals();
});

const REASONS: PairReason[] = ['wrong_code', 'expired', 'too_many_tries', 'paused', 'busy', 'network', 'server', 'incompatible', 'too_old', 'local', 'not_local'];

describe('shellInfo', () => {
	it('is null in a browser or the phone app, where nothing injects it', () => {
		expect(shellInfo()).toBeNull();
	});
	it('reads local default-workspace and remote modes from the injected object', () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = {
			mode: 'local', default_workspace: { path: '/fixture/default-workspace', display: '~/CORSIA' },
		};
		expect(shellInfo()).toEqual({
			mode: 'local', default_workspace: { path: '/fixture/default-workspace', display: '~/CORSIA' },
		});
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		expect(shellInfo()).toEqual({ mode: 'remote', name: 'Studio' });
	});
	it('does not accept an incomplete default workspace as a path', () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = {
			mode: 'local', default_workspace: { path: '~/CORSIA', display: 42 },
		};
		expect(shellInfo()).toEqual({ mode: 'local' });
	});
	it('treats a remote object with no name as local, never a blank computer', () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote' };
		expect(shellInfo()).toEqual({ mode: 'local' });
	});
});

describe('the shell commands', () => {
	it('asks for a recovery start only, never the full start, and passes the typed answers through', async () => {
		const invoke = vi.fn(async () => ({ recovery: 'ready', url: 'http://127.0.0.1:1956' }));
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		expect(await recoverLocalService('automatic')).toEqual({ recovery: 'ready', url: 'http://127.0.0.1:1956' });
		expect(await recoverLocalService('manual')).toEqual({ recovery: 'ready', url: 'http://127.0.0.1:1956' });
		expect(invoke.mock.calls).toEqual([['start_local_service', { recovery: 'automatic' }], ['start_local_service', { recovery: 'manual' }]]);
		for (const recovery of ['deferred', 'manual_available', 'unavailable', 'not_started'] as const) {
			vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ recovery })) });
			expect(await recoverLocalService('automatic')).toEqual({ recovery });
		}
	});
	it.each([{ state: 'ready', url: 'http://127.0.0.1:1956' }, { state: 'requires_approval' }, { recovery: 'ready', url: '' }, { recovery: 'ready', url: 'https://evil.example' }, { recovery: 'started' }, null, 'started'])('treats a full-start or malformed answer as unavailable: %j', async (response) => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => response) });
		expect(await recoverLocalService('automatic')).toEqual({ recovery: 'unavailable' });
	});
	it('treats a refused or missing shell as unavailable because it proves no admission', async () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => { throw new Error('Only CORSIA’s startup window can start CORSIA.'); }) });
		expect(await recoverLocalService('manual')).toEqual({ recovery: 'unavailable' });
		vi.unstubAllGlobals();
		expect(await recoverLocalService('manual')).toEqual({ recovery: 'unavailable' });
	});
	it('reports one fixed diagnostic for rejected or malformed recovery IPC without exposing its input', async () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ recovery: 'deferred' })) });
		await recoverLocalService('automatic');
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => { throw new Error('private recovery failure: /fixture/secret'); }) });
		expect(await recoverLocalService('automatic')).toEqual({ recovery: 'unavailable' });
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ recovery: 'unexpected', detail: 'private recovery failure: /fixture/secret' })) });
		expect(await recoverLocalService('manual')).toEqual({ recovery: 'unavailable' });
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledWith('[update] recovery start is unavailable');
	});
	it('is never the shell update page in the app the service serves', () => {
		expect(onStartupShellPage()).toBe(false);
	});
	it('calls remote_discovered, remote_pair and disconnect_remote with the contract arguments', async () => {
		const invoke = vi.fn(async (command: string) => command === 'remote_discovered' ? { names: ['Studio'] } : command === 'remote_pair' ? { ok: true, name: 'Studio' } : undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		expect(await remoteDiscovered()).toEqual({ names: ['Studio'], denied: false });
		expect(await remotePair({ kind: 'dns_sd', instance: 'Studio' }, 'K7QX')).toEqual({ ok: true, name: 'Studio' });
		await disconnectRemote();
		expect(invoke).toHaveBeenCalledWith('remote_pair', { target: { kind: 'dns_sd', instance: 'Studio' }, code: 'K7QX' });
		expect(invoke).toHaveBeenCalledWith('disconnect_remote');
	});
	// The shell's `remote_pair_accepts_the_arguments_the_page_sends` invokes the real command with these same arguments.
	it.each(pairRequests)('sends remote_pair the arguments the shell accepts: %j', async (request) => {
		const invoke = vi.fn(async () => ({ ok: true, name: 'Studio' }));
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		await remotePair(request.target as Parameters<typeof remotePair>[0], request.code);
		expect(invoke).toHaveBeenCalledWith('remote_pair', request);
	});
	it.each(REASONS)('passes the %s refusal through unchanged, with versions', async (reason) => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ ok: false, reason, app_version: '1.4', host_version: '1.2' })) });
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason, app_version: '1.4', host_version: '1.2' });
	});
	it('reports a shell that is missing, refuses or fails after the code was used as a local failure, never the network or a wrong code', async () => {
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason: 'local' });
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => { throw new Error('denied'); }) });
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason: 'local' });
	});
	it('keeps the network reason for a real network failure the shell reports', async () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ ok: false, reason: 'network' })) });
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason: 'network' });
	});
	it('says the code was used and asks for a new one when this computer could not finish', () => {
		expect(unreachableLine('local', 'Studio', {})).toBe(`CORSIA couldn’t finish connecting to Studio. ${SETTINGS_PATH} on Studio to get a new code and try again.`);
		expect(refusesOnCodeScreen('local')).toBe(false);
	});
	it('reports an unknown reason as the network too', async () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ ok: false, reason: 'surprise' })) });
		expect((await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX'))).toEqual({ ok: false, reason: 'network' });
	});
});

describe('parseAddress', () => {
	it.each([
		['studio.local', { host: 'studio.local' }],
		['  studio.local  ', { host: 'studio.local' }],
		['192.168.1.20:47902', { host: '192.168.1.20', port: 47902 }],
		['[fd5c::1]:47902', { host: 'fd5c::1', port: 47902 }],
		['fd5c:9e1a::1a', { host: 'fd5c:9e1a::1a' }],
	])('reads %s', (text, expected) => expect(parseAddress(text)).toEqual(expected));
	it.each(['', 'two words', 'studio.local:0', 'studio.local:99999', ':47901'])('refuses %j', (text) => expect(parseAddress(text)).toBeNull());
});

describe('the approved lines', () => {
	it('every refusal that names Settings sends the person to Open CORSIA › Settings › System', () => {
		for (const reason of REASONS) {
			const line = codeRefusalLine(reason, 'Studio');
			if (line?.includes('Settings')) expect(line).toContain(SETTINGS_PATH);
		}
		expect(SETTINGS_PATH.replace(/ /g, ' ')).toBe('Open CORSIA › Settings › System');
	});
	it('words each code refusal for its cause', () => {
		expect(codeRefusalLine('wrong_code', 'Studio')).toMatch(/^That code doesn’t match\./);
		expect(codeRefusalLine('expired', 'Studio')).toMatch(/^That code expired\./);
		expect(codeRefusalLine('too_many_tries', 'Studio')).toMatch(/^Too many wrong codes\./);
		expect(codeRefusalLine('paused', 'Studio')).toMatch(/^Pairing is paused on Studio\./);
		expect(codeRefusalLine('busy', 'Studio')).toContain('Studio is busy');
		expect(codeRefusalLine('not_local', 'Studio')).toBe('If this computer isn’t on the same network as the main computer, connect it to that network, then try again.');
	});
	it('splits refusals between the code screen and the choose screen', () => {
		expect(REASONS.filter(refusesOnCodeScreen)).toEqual(['wrong_code', 'expired', 'too_many_tries', 'paused', 'busy', 'not_local']);
	});
	it('says what is wrong with the computer, for the choose screen', () => {
		expect(unreachableLine('server', 'studio.local', {})).toBe('studio.local answered, but CORSIA isn’t running there.');
		// Setup's approved r4 frame has one line for both; no network wording is approved.
		expect(unreachableLine('network', 'studio.local', {})).toBe('CORSIA can’t reach studio.local. Check that it’s on and on the same network as this computer.');
		expect(unreachableLine('incompatible', 'Studio', { app: '1.4', host: '1.2' })).toBe('This computer has CORSIA 1.4 and Studio has 1.2. Update the older one, then try again.');
		expect(unreachableLine('too_old', 'Studio', {})).toBe('Studio has an older CORSIA. Update it, then try again.');
		expect(unreachableLine('wrong_code', 'Studio', {})).toBeNull();
	});
});
