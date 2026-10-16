import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	SETTINGS_PATH, codeRefusalLine, disconnectRemote, parseAddress, refusesOnCodeScreen, remoteDiscovered, remotePair, shellInfo,
	unreachableLine, type PairReason,
} from './remoteShell';

afterEach(() => {
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
	vi.unstubAllGlobals();
});

const REASONS: PairReason[] = ['wrong_code', 'expired', 'too_many_tries', 'paused', 'busy', 'network', 'server', 'incompatible', 'too_old'];

describe('shellInfo', () => {
	it('is null in a browser or the phone app, where nothing injects it', () => {
		expect(shellInfo()).toBeNull();
	});
	it('reads local and remote modes from the injected object', () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
		expect(shellInfo()).toEqual({ mode: 'local' });
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		expect(shellInfo()).toEqual({ mode: 'remote', name: 'Studio' });
	});
	it('treats a remote object with no name as local, never a blank computer', () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote' };
		expect(shellInfo()).toEqual({ mode: 'local' });
	});
});

describe('the shell commands', () => {
	it('calls remote_discovered, remote_pair and disconnect_remote with the contract arguments', async () => {
		const invoke = vi.fn(async (command: string) => command === 'remote_discovered' ? { names: ['Studio'] } : command === 'remote_pair' ? { ok: true, name: 'Studio' } : undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		expect(await remoteDiscovered()).toEqual({ names: ['Studio'], denied: false });
		expect(await remotePair({ kind: 'dns_sd', instance: 'Studio' }, 'K7QX')).toEqual({ ok: true, name: 'Studio' });
		await disconnectRemote();
		expect(invoke).toHaveBeenCalledWith('remote_pair', { target: { kind: 'dns_sd', instance: 'Studio' }, code: 'K7QX' });
		expect(invoke).toHaveBeenCalledWith('disconnect_remote');
	});
	it.each(REASONS)('passes the %s refusal through unchanged, with versions', async (reason) => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => ({ ok: false, reason, app_version: '1.4', host_version: '1.2' })) });
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason, app_version: '1.4', host_version: '1.2' });
	});
	it('reports a shell that is missing or throws as the network, never as a wrong code', async () => {
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason: 'network' });
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => { throw new Error('denied'); }) });
		expect(await remotePair({ kind: 'address', host: 'studio.local' }, 'K7QX')).toEqual({ ok: false, reason: 'network' });
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
	});
	it('splits refusals between the code screen and the choose screen', () => {
		expect(REASONS.filter(refusesOnCodeScreen)).toEqual(['wrong_code', 'expired', 'too_many_tries', 'paused', 'busy']);
	});
	it('says what is wrong with the computer, for the choose screen', () => {
		expect(unreachableLine('server', 'studio.local', {})).toBe('studio.local answered, but CORSIA isn’t running there.');
		expect(unreachableLine('network', 'studio.local', {})).toContain('can’t reach studio.local');
		expect(unreachableLine('incompatible', 'Studio', { app: '1.4', host: '1.2' })).toBe('This computer has CORSIA 1.4 and Studio has 1.2. Update the older one, then try again.');
		expect(unreachableLine('too_old', 'Studio', {})).toBe('Studio has an older CORSIA. Update it, then try again.');
		expect(unreachableLine('wrong_code', 'Studio', {})).toBeNull();
	});
});
