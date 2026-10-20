<script lang="ts">
	import { tick } from 'svelte';
	import { Button, CodeInput, Field, RailRow, Spinner } from '$lib/components/ui';
	import SetupHostSteps from './SetupHostSteps.svelte';
	import type { SetupRemote } from '$lib/setupRemote.svelte';
	import { openExternalUrl } from '$lib/linkTargetResolver';
	import { SETTINGS_PATH_IN_SENTENCE as openPath, SYSTEM_SETTINGS_LOCAL_NETWORK } from '$lib/remoteShell';

	/**
	 * Connect to CORSIA on another computer, setup's r1 to r7. Choose has three states: Looking (one line, no address
	 * field; the spinner line and a secondary same-network line), Found (the computers on this network as sidebar-style rows, names only, the picked one selected, with a quiet
	 * link that reveals the address field), and Nothing found (both computers must be on one network, the Settings row to turn
	 * on, Look again as Continue's place, and the same link). Pairing is same-network only. Code: the four characters that
	 * computer shows in its Settings, typed one per box; the fourth connects, so there is no button. A refusal clears the boxes and puts the cursor
	 * back in the first one.
	 */
	const { remote }: { remote: SetupRemote } = $props();

	let list = $state<HTMLElement>();
	let more = $state(false);
	function listEdges() {
		if (list) more = list.scrollTop + list.clientHeight < list.scrollHeight - 1;
	}
	$effect(() => { void remote.names.length; void tick().then(listEdges); });

	// r7's content (picture, boxes, one line) scrolls above the fixed dock when a long name makes it tall. A refusal line is
	// the last thing in the pane and is scrolled to the nearest edge, so the picture is cut only as far as needed.
	let pane = $state<HTMLElement>();
	let refusalLine = $state<HTMLElement>();
	let moreBelow = $state(false);
	let moreAbove = $state(false);
	function paneEdges() {
		if (!pane) return;
		moreAbove = pane.scrollTop > 1;
		moreBelow = pane.scrollTop + pane.clientHeight < pane.scrollHeight - 1;
	}
	$effect(() => {
		if (!pane) return;
		const observer = new ResizeObserver(paneEdges);
		observer.observe(pane);
		for (const child of pane.children) observer.observe(child);
		paneEdges();
		return () => observer.disconnect();
	});
	$effect(() => {
		if (remote.refusal) void tick().then(() => { refusalLine?.scrollIntoView({ block: 'nearest' }); paneEdges(); });
	});

	const addressError = $derived(remote.chooseError || (remote.invalidAddress ? 'Type a name or an address, like studio.local or 192.168.1.20.' : undefined));
	const hasError = $derived(Boolean(remote.chooseError));
</script>

{#if remote.stage === 'code'}
	<div class="scroll" class:more={moreBelow} class:above={moreAbove} bind:this={pane} onscroll={paneEdges}>
		<SetupHostSteps name={remote.label} show={remote.typedTarget ? ['open', 'code'] : undefined} />
		<div class="code">
			<CodeInput label="Code" bind:value={remote.code} invalid={remote.invalid} disabled={remote.busy} refocus={remote.refocus} focusAt={0} onsubmit={(entered) => void remote.submit(entered)} />
			{#if remote.refusal}
				<p class="err" role="alert" bind:this={refusalLine}>{remote.refusal}</p>
			{:else if remote.busy || remote.connectedTo}
				<p class="state" aria-busy="true"><Spinner size={12} />Connecting to {remote.connectedTo || remote.label}…</p>
			{/if}
		</div>
	</div>
{:else}
	{#if remote.phase === 'found'}
		<h2 class="sub">Computers on this network</h2>
		<ul class="list" class:more aria-label="CORSIA on this network" bind:this={list} onscroll={listEdges}>
			{#each remote.names as name (name)}
				<li><RailRow label={name} iconName="computer" state={name === remote.picked ? 'current' : 'idle'} onclick={() => remote.pick(name)} /></li>
			{/each}
		</ul>
	{:else if remote.phase === 'denied'}
		<p class="sentence">CORSIA can’t look on this network. Allow it in Privacy &amp; Security › Local Network. It looks again when you come back to this window.</p>
		<div class="action"><Button variant="chip" size="sm" onclick={() => void openExternalUrl(SYSTEM_SETTINGS_LOCAL_NETWORK)}>Open System Settings</Button></div>
	{:else if remote.phase === 'none'}
		<h2 class="sub">No computers found on this network.</h2>
		<p class="sentence">Both computers need to be on the same network. On the main computer, {openPath} and turn on Let other devices connect.</p>
		<div class="crop"><SetupHostSteps show={['on']} large /></div>
	{:else}
		<p class="state" aria-busy="true"><Spinner size={12} />Looking for computers on this network…</p>
		<p class="note">Both computers need to be on the same network.</p>
	{/if}

	{#if remote.phase !== 'looking'}
		{#if remote.addressOpen}
			<div class="address">
				<Field variant="chip" layout="stacked" label="Or type its address" labelFor="remote-address" error={addressError}>
					<input id="remote-address" type="text" placeholder="Name or IP address" autocapitalize="off" autocomplete="off" spellcheck="false" aria-invalid={hasError || remote.invalidAddress} bind:value={remote.address} />
				</Field>
			</div>
		{:else}
			<div class="action"><Button variant="ghost" size="sm" onclick={() => remote.showAddress()}>Not listed? Type its address</Button></div>
		{/if}
	{/if}
{/if}

<style>
	/* As tall as its rows, up to four, so the link below sits right under the last computer. */
	.list { list-style: none; margin: 0 0 var(--space-4); padding: 0; max-height: calc(var(--space-8) * 4); overflow-y: auto; flex: none; }
	.note { margin: calc(var(--space-2) * -1) 0 var(--space-4); font-size: var(--text-footnote); color: var(--text-secondary); }
	.list.more { mask-image: linear-gradient(to bottom, currentColor calc(100% - var(--space-6)), transparent); }
	.state { margin: 0 0 var(--space-4); display: flex; align-items: center; gap: var(--space-3); font-size: var(--text-subhead); color: var(--text-secondary); }
	.sentence { margin: 0 0 var(--space-3); max-width: calc(var(--space-8) * 9); font-size: var(--text-body); line-height: var(--lh-normal); text-wrap: pretty; }
	.action { margin: 0 0 var(--space-4); }
	.sub { margin: 0 0 var(--space-5); font-size: var(--text-body); font-weight: var(--fw-semibold); }
	.crop { margin: 0 0 var(--space-4); }
	.address { max-width: calc(var(--space-8) * 8); }
	/* The boxes sit one --space-5 under the picture, so they read as its answer. */
	.scroll {
		height: 100%; min-height: 0; overflow-y: auto;
		/* Room at the top and sides for the boxes' focus ring and shadow, which an overflow box would clip; the margin takes it back. */
		margin: calc(var(--space-4) * -1) calc(var(--space-4) * -1) calc(var(--space-2) * -1); padding: var(--space-4) var(--space-4) 0;
	}
	.scroll.more { mask-image: linear-gradient(to bottom, currentColor calc(100% - var(--space-6)), transparent); }
	.scroll.above { mask-image: linear-gradient(to bottom, transparent, currentColor var(--space-6)); }
	.scroll.above.more { mask-image: linear-gradient(to bottom, transparent, currentColor var(--space-6), currentColor calc(100% - var(--space-6)), transparent); }
	.code { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-5); }
	.err { margin: 0; font-size: var(--text-footnote); color: var(--red-text); text-wrap: pretty; }
</style>
