<script lang="ts">
	import SettingsDevicesView from './SettingsDevicesView.svelte';
	import { EMPTY_VIEW, type DevicesView } from '$lib/devices.svelte';
	import { SETTINGS_PATH } from '$lib/remoteShell';

	/**
	 * What to do on the other computer, as numbered pictures of its own screen: Settings › System, the Let other devices
	 * connect row switched on, and the row showing a code. The row is the component Settings renders, scaled down and
	 * inert, with four dashes where the code appears so there is nothing here to type by mistake. Not one card in a
	 * window frame: each step is its own labelled crop. `show` picks the crops (numbered in the order shown): all three for
	 * a discovered computer, only the row switched on for the nothing-found screen, and without that one for a typed address,
	 * which answers only when sharing is already on.
	 */
	type Step = 'open' | 'on' | 'code';
	const { name = '', typed = false, show = ['open', 'on', 'code'], large = false }: { name?: string; typed?: boolean; show?: Step[]; large?: boolean } = $props();

	const base: DevicesView = { ...EMPTY_VIEW, load: 'ready', enabled: true, announced: true, hostName: 'This computer', addresses: [] };
	// A typed address is not the computer's name, so the crops never paint it as "Visible as".
	const shown = $derived(typed ? base.hostName : name);
	const on = $derived<DevicesView>({ ...base, hostName: shown });
	const code = $derived<DevicesView>({ ...base, hostName: shown, pairing: 'code', code: '––––', endsInSeconds: 540 });
</script>

<ol class="steps" class:large data-testid="host-steps" aria-hidden="true" inert>
	{#each show as step, index (step)}
		<li>
			<span class="n">{index + 1}</span>
			<div class="pic">
				{#if step === 'open'}
					<span class="label">{SETTINGS_PATH}</span>
					<div class="crop head"><b>System</b><span>Controls and health for this computer.</span></div>
				{:else if step === 'on'}
					<span class="label">Turn on Let other devices connect</span>
					<div class="crop row"><SettingsDevicesView view={{ ...on, pairing: 'idle' }} /></div>
				{:else}
					<span class="label">Press New code</span>
					<div class="crop row"><SettingsDevicesView view={code} /></div>
				{/if}
			</div>
		</li>
	{/each}
</ol>

<style>
	.steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); max-width: calc(var(--space-8) * 9); pointer-events: none; user-select: none; }
	li { display: flex; align-items: flex-start; gap: var(--space-3); }
	.n {
		flex: none; width: var(--space-5); height: var(--space-5); display: grid; place-items: center;
		border-radius: var(--radius-circle); background: var(--accent-wash); color: var(--accent-text);
		font-size: var(--text-subhead); font-weight: var(--fw-semibold);
	}
	.pic { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); }
	.label { color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.crop { border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary); overflow: hidden; }
	/* The row is laid out at the width Settings gives it, then the whole crop is scaled by one factor. */
	.row { zoom: 0.55; padding: var(--space-3) var(--space-3) 0; }
	.row :global(.settings-card) { margin-bottom: var(--space-3); }
	/* The only picture on its screen (nothing found), so it is drawn larger. */
	.large .row { zoom: 0.85; }
	.head { zoom: 0.8; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-1); }
	.head b { font-size: var(--text-title2); font-weight: var(--fw-semibold); color: var(--text-primary); }
	.head span { font-size: var(--text-subhead); color: var(--text-muted); }
</style>
