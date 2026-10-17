<script lang="ts">
	import { Button, RailRow, Spinner, Toggle } from '$lib/components/ui';
	import { needsSpace, type LocalPick } from '$lib/setupLocalPick.svelte';
	import { welcomeModelMeta } from '$lib/setupLocalPresentation';
	import { MANAGE_DISCLOSURE, MANAGE_DISCLOSURE_LEAD, MANAGE_LABEL, MANAGE_META, OWN_OMLX_FOUND, USE_MINE_LABEL, USE_MINE_META } from '$lib/ownOmlxCopy';

	interface Props {
		localEligible?: boolean;
		freeDiskGb?: number;
		pick: LocalPick | null;
		downloadEtaSeconds?: number | null;
		downloadStalled?: boolean;
		wanted: boolean;
		onWantedChange: (next: boolean) => void;
		onRecheck: () => void;
		onRetry: () => void;
		/** The person runs their own oMLX (J9): both choices use it; `manage` is Let CORSIA manage it. */
		ownOmlx?: boolean;
		manage?: boolean;
		onManageChange?: (next: boolean) => void;
	}
	const { localEligible, freeDiskGb, pick, downloadEtaSeconds, downloadStalled, wanted, onWantedChange, onRecheck, onRetry, ownOmlx = false, manage = true, onManageChange }: Props = $props();
	// Nothing fits is told the same way as too little memory: there is no local model for this computer.
	const noModel = $derived(localEligible === false || pick?.state === 'low_memory' || pick?.state === 'none_fits');
	const modelSize = $derived(pick?.model ? `${pick.model.download_size_gb} GB` : '');
</script>

<div class="welcome-copy">
	{#if noModel}
		<p>This computer doesn’t have enough memory to run an AI model itself. CORSIA will use Claude Code or Codex with your subscription, or an API key.</p>
	{:else if needsSpace(pick, freeDiskGb)}
		<p>Needs {modelSize} free; {freeDiskGb} GB is free.</p>
		<Button variant="chip" onclick={onRecheck}>Free up space, then check again</Button>
	{:else}
		<div class="download-choice">
			<span class="switch"><Toggle checked={wanted} onchange={onWantedChange} ariaLabel="Also run AI on this computer" /></span>
			<div class="choice-copy">
				<span class="choice-name">Also run AI on this computer</span>
				<span class="why">Free, private, and works offline. It’s slower than Claude or ChatGPT, so it’s for work you’d rather keep on this computer, and you can move background jobs onto it.</span>
				{#if ownOmlx}
					<span class="spec">{OWN_OMLX_FOUND}</span>
				{:else if pick?.state === 'checking'}
					<span class="spec checking"><Spinner size={12} />Finding the model that fits this computer…</span>
				{:else if pick?.state === 'failed'}
					<span class="spec">CORSIA couldn’t check which local AI model fits this computer.</span>
					<span class="spec">You can go on and add a local model from the app later.</span>
					<span class="retry"><Button variant="chip" size="sm" onclick={onRetry}>Try again</Button></span>
				{:else if pick?.model}
					<span class="spec">{welcomeModelMeta(pick.model.name, modelSize, downloadEtaSeconds, downloadStalled)}</span>
				{/if}
				{#if pick?.state !== 'failed' && !ownOmlx}
					<span class="spec">The download starts in the background when you press Get started.</span>
				{/if}
			</div>
		</div>
		{#if ownOmlx && wanted}
			<div class="picks" role="group" aria-label="Your oMLX">
				<RailRow label={MANAGE_LABEL} state={manage ? 'current' : 'idle'} meta={MANAGE_META} onclick={() => onManageChange?.(true)}>
					{#snippet icon()}{#if manage}{@render radioOn()}{:else}{@render radioOff()}{/if}{/snippet}
				</RailRow>
				<RailRow label={USE_MINE_LABEL} state={manage ? 'idle' : 'current'} meta={USE_MINE_META} onclick={() => onManageChange?.(false)}>
					{#snippet icon()}{#if !manage}{@render radioOn()}{:else}{@render radioOff()}{/if}{/snippet}
				</RailRow>
			</div>
			<p class="disclose"><strong>{MANAGE_DISCLOSURE_LEAD}</strong> {MANAGE_DISCLOSURE}</p>
		{/if}
	{/if}
</div>

{#snippet radioOn()}
	<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" /></svg>
{/snippet}
{#snippet radioOff()}
	<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8" /></svg>
{/snippet}

<style>
	.welcome-copy { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.welcome-copy p { margin: 0; }
	.download-choice { display: flex; align-items: flex-start; gap: var(--space-3); }
	.switch { flex: none; display: inline-flex; padding-top: var(--space-1); }
	.choice-copy { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
	.choice-name { color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-medium); }
	.why { color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); text-wrap: pretty; }
	.spec { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-normal); text-wrap: pretty; }
	.checking { display: inline-flex; align-items: center; gap: var(--space-2); }
	.retry { align-self: flex-start; padding-top: var(--space-1); }
	.picks { display: flex; flex-direction: column; gap: var(--space-1); }
	.disclose { margin: 0; font-size: var(--text-footnote); color: var(--text-secondary); line-height: var(--lh-normal); text-wrap: pretty; }
	.disclose strong { font-weight: var(--fw-medium); color: var(--text-primary); }
</style>
