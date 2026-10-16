<script lang="ts">
	import type { UsageSegment } from '$lib/stores/modelGroups';
	import type { VendorAccount } from '$lib/stores/vendorAccounts';

	interface Props {
		vendor: string;
		account?: VendorAccount | null;
		segments: UsageSegment[];
		chooser?: boolean;
		open?: boolean;
		rollover?: boolean;
		recovery?: VendorAccount | null;
		onchoose?: () => void;
		actionLabel?: string;
		onaction?: () => void;
		breakdown?: string[];
		usageId?: string;
		usageOpen?: boolean;
		ontoggleusage?: () => void;
		ontoggle?: () => void;
	}

	let {
		vendor,
		account = null,
		segments,
		chooser = false,
		open = false,
		rollover = false,
		recovery = null,
		onchoose,
		actionLabel,
		onaction,
		breakdown = [],
		usageId = 'picker-account-usage',
		usageOpen = false,
		ontoggleusage,
		ontoggle,
	}: Props = $props();
	const usageLabel = $derived(segments.every((segment) => segment.label) ? segments.map((segment) => segment.label).join(', ') : undefined);
</script>

<div class="picker-account-header">
	<div class="picker-account-line">
		{#if chooser}
			<button type="button" class="picker-account-title press-tint" aria-expanded={open} onclick={ontoggle}>
				<span class="vendor">{vendor}</span>
				{#if account}
					<span class="separator" aria-hidden="true">{' · '}</span>
					<span class="account-name">{rollover ? `Auto · ${account.label.name}` : account.label.name}</span>
				{/if}
				<span class="chooser-caret" class:open aria-hidden="true"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span>
			</button>
		{:else}
			<span class="picker-account-title"><span class="vendor">{vendor}</span>{#if account}<span class="separator" aria-hidden="true">{' · '}</span><span class="account-name">{account.label.name}</span>{/if}</span>
		{/if}
		{#if segments.length > 0}
			{#if breakdown.length > 0}
				<button type="button" class="usage press-tint" aria-label={usageLabel} aria-expanded={usageOpen} aria-controls={usageId} onclick={ontoggleusage}>
					{#each segments as segment, index}
						{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span aria-hidden={usageLabel ? 'true' : undefined} class:amber={segment.level === 'amber'} class:red={segment.level === 'red'}>{segment.text}</span>
					{/each}
				</button>
			{:else}
				<span class="usage" role="group" aria-label={usageLabel}>
					{#each segments as segment, index}
						{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span aria-hidden={usageLabel ? 'true' : undefined} class:amber={segment.level === 'amber'} class:red={segment.level === 'red'}>{segment.text}</span>
					{/each}
				</span>
			{/if}
		{/if}
		{#if recovery && onchoose}
			<button type="button" class="recovery" onclick={onchoose}>Use {recovery.label.name}</button>
		{:else if actionLabel && onaction}
			<button type="button" class="recovery" onclick={onaction}>{actionLabel}</button>
		{/if}
	</div>
	{#if usageOpen && breakdown.length > 0}
		<div class="picker-account-usage" id={usageId}>
			{#each breakdown as line}<div>{line}</div>{/each}
		</div>
	{/if}
</div>

<style>
	.picker-account-header { padding: 6px 10px 4px; color: var(--text-primary); }
	.picker-account-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 8px; min-height: 28px; font-size: var(--text-footnote); }
	.picker-account-title { display: flex; min-width: 0; align-items: baseline; gap: 3px; padding: 0; color: inherit; font: inherit; background: none; border: 0; border-radius: var(--radius-xs); text-align: left; }
	button.picker-account-title { cursor: pointer; }
	.vendor { flex: 0 0 auto; font-size: var(--text-subhead); font-weight: 600; white-space: nowrap; }
	.separator, .usage { color: var(--text-tertiary); }
	button.usage { padding: 0; font: inherit; background: none; border: 0; cursor: pointer; }
	.account-name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.chooser-caret { display: flex; flex: 0 0 auto; align-self: center; margin-left: auto; padding-left: 4px; color: var(--text-tertiary); }
	.chooser-caret svg { display: block; transition: transform var(--dur-tight); }
	.chooser-caret.open svg { transform: rotate(180deg); }
	.usage { min-width: 0; font-variant-numeric: tabular-nums; }
	.usage .amber { color: var(--amber-text); }
	.usage .red { color: var(--red-text); }
	.recovery { padding: 0; color: var(--accent-text); font: inherit; font-size: var(--text-footnote); background: none; border: 0; cursor: pointer; }
	.picker-account-usage { padding: 2px 0 2px; font-size: var(--text-caption); color: var(--text-tertiary); }
	@media (max-width: 480px) {
		.picker-account-title { flex-basis: 100%; }
	}
</style>
