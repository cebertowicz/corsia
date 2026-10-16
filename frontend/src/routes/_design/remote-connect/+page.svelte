<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import SetupScreen from '$lib/components/SetupScreen.svelte';
	import SettingsDevices from '$lib/components/SettingsDevices.svelte';

	/**
	 * Frames for corsia#638 PR 2. The real screens run on their own code path; only the outside is faked, the way a test
	 * fakes it: the desktop shell (`window.__CORSIA_SHELL__` and its commands) and the server answers (`fetch`). The
	 * frame script reaches each state by using the screens (click, type), never by editing the page.
	 *   ?kind=setup&discover=found|empty|denied&pair=ok|wrong_code|expired|too_many_tries|paused|busy|server|network|incompatible|too_old
	 *   ?kind=devices&state=off|on|code|ending|not-announced|paused|starting|ports-taken|load-failed|loading|read-only|long-name|long-name-read-only
	 *   &theme=light|dark
	 */
	const query = page.url.searchParams;
	const kind = query.get('kind') ?? 'setup';
	const discover = query.get('discover') ?? 'found';
	const pair = query.get('pair') ?? 'ok';
	const which = query.get('state') ?? 'on';
	const theme = query.get('theme') ?? 'light';

	const LONG = 'Steve’s Mac Studio (2)';
	const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
	const server = (patch: Record<string, unknown> = {}, name: string | null = 'Studio') => ({
		enabled: true, listening: { port: 47901 }, announced_name: name, local_hostname: 'studio.local',
		lan_addresses: ['192.168.1.20'], code: { state: 'none' }, ...patch,
	});
	const active = (seconds: number) => ({ state: 'active', value: 'K7QX', expires_in_seconds: seconds });

	function installDevices() {
		const remote = which === 'read-only' || which === 'long-name-read-only';
		const long = which.startsWith('long-name');
		const name = long ? LONG : 'Studio';
		const byState: Record<string, () => Response | Promise<Response>> = {
			off: () => json(server({ enabled: false }, null)),
			on: () => json(server({}, name)),
			code: () => json(server({ code: active(540) }, name)),
			ending: () => json(server({ code: active(42) }, name)),
			'not-announced': () => json(server({}, null)),
			paused: () => json(server({ code: { state: 'paused' } }, name)),
			starting: () => json(server({ enabled: false }, null)),
			'ports-taken': () => json(server({ enabled: false, listening: { error: 'No approved Devices port is available' } }, null)),
			'load-failed': () => json({}, 500),
			loading: () => new Promise<Response>(() => {}),
			'read-only': () => json(server({ code: { state: 'active', expires_in_seconds: 300 } }, name)),
			'long-name': () => json(server({}, name)),
			'long-name-read-only': () => json(server({}, name)),
		};
		const read = byState[which] ?? byState.on;
		window.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
			// "starting": the switch turns on and the host is still starting; the answer never comes during the frame.
			if (init?.method === 'PUT') return new Promise<Response>(() => {});
			return read();
		}) as typeof fetch;
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = remote ? { mode: 'remote', name } : { mode: 'local' };
	}

	function installSetup() {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
		const names = discover === 'found' ? ['Steve’s Mac Studio', 'Living room mini'] : [];
		(window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
			invoke: async (command: string) => {
				if (command === 'remote_discovered') return { names, denied: discover === 'denied' };
				if (command === 'remote_pair') {
					return pair === 'ok' ? { ok: true, name: 'Steve’s Mac Studio' }
						: pair === 'incompatible' ? { ok: false, reason: pair, app_version: '1.4', host_version: '1.2' }
						: { ok: false, reason: pair };
				}
				return undefined;
			},
		};
	}

	let ready = $state(false);
	onMount(() => {
		document.documentElement.setAttribute('data-theme', theme);
		if (kind === 'devices') installDevices(); else installSetup();
		ready = true;
	});
</script>

{#if ready}
	{#if kind === 'devices'}
		<main class="settings-frame" data-frame={`devices-${which}`}><SettingsDevices /></main>
	{:else}
		<div class="setup-frame" data-frame="setup"><SetupScreen startConnect onExistingAccount={() => {}} onAccountCreated={() => {}} onFinished={() => {}} onLeaveConnect={() => {}} /></div>
	{/if}
{/if}

<style>
	.settings-frame { box-sizing: border-box; min-height: 100vh; padding: var(--space-6, 24px) var(--space-4, 16px); background: var(--bg-primary); }
	.setup-frame { min-height: 100vh; }
</style>
