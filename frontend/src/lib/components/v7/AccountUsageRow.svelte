<script lang="ts">
	import type { Snippet } from 'svelte';
	import { accountSegments, accountSpendingSegment, accountWindowSegment } from '$lib/stores/modelGroups';
	import { formatUsageLimit } from '$lib/stores/overageSetting';
	import type { VendorAccount } from '$lib/stores/vendorAccounts';
	import CurrentCheck from '../CurrentCheck.svelte';
	import { inlineSlot } from '$lib/slotMotion';
	import SwitchingIcon from '../ui/SwitchingIcon.svelte';

	/**
	 * The one account row. Settings renders it in full (`settings`, or `compact` in a policy list);
	 * the model picker renders the same account as one compact line (`picker`): the label, what is
	 * left per window or the one state that replaces it, a fill on the account the next turn runs
	 * on, and, while rollover is on, its position. With `onselect` the picker line is a button that
	 * pins the thread to it; without it (a draft chat has no thread to pin) the line is read-only.
	 */
	interface Props {
		account: VendorAccount;
		variant?: 'settings' | 'picker';
		/** Settings: the one-line form used in a policy list. */
		compact?: boolean;
		/** Rollover position, shown only while rollover is on. */
		order?: number | null;
		/** A short account marker that stays with the account name. */
		marker?: string | null;
		/** Settings: controls beside the row. */
		children?: Snippet;
		/** Picker: the account the next turn runs on. */
		marked?: boolean;
		/** Picker: a limited or signed-out account cannot be chosen. */
		choosable?: boolean;
		/** Picker: chosen during a running turn; the next turn moves to it, as a model switch does. */
		pending?: boolean;
		onselect?: () => void;
	}

	const { account, variant = 'settings', compact = false, order = null, marker = null, children, marked = false, choosable = true, pending = false, onselect }: Props = $props();
	const label = $derived(account.label);
	const hasEmail = $derived(!!label.email && label.email !== label.name);
	const usageLimit = $derived(account.usage?.state === 'limited' ? formatUsageLimit(account.usage) : null);
	const checking = $derived(!!account.usage?.checking);
	const checkError = $derived(account.usage?.check?.error ? "Couldn't verify account" : null);
	const spending = $derived(accountSpendingSegment(account));
	const segments = $derived(spending ? [spending] : (account.usage?.windows ?? []).map(accountWindowSegment));
	const usageLabel = $derived(segments.map((segment) => segment.label ?? segment.text).join(', '));
	const pickerSegments = $derived(accountSegments(account));
	const full = $derived(hasEmail ? `${label.name} · ${label.email}` : label.name);
</script>

{#snippet pickerBody()}
	{#if order !== null}<span class="order" aria-label={`Rollover position ${order}`}>{order}</span>{/if}
	<span class="main">
		<span class="picker-label" title={full}>
			<span class="name-group"><span class="name">{label.name}</span>{#if pending}<span class="switching-slot" in:inlineSlot out:inlineSlot><SwitchingIcon ariaLabel="Switching account" /></span>{/if}</span>{#if hasEmail}<span class="sep" aria-hidden="true">{' · '}</span><span class="email">{label.email}</span>{/if}
		</span>
		<span class="figures">
			{#each pickerSegments as segment, index}
				{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}<span class:amber={segment.level === 'amber'} class:red={segment.level === 'red'} aria-label={segment.label}>{segment.text}</span>
			{/each}
		</span>
	</span>
	{#if marked}<CurrentCheck />{/if}
{/snippet}

{#if variant === 'picker'}
	{#if onselect}
		<button type="button" class="account-usage-row picker press-tint" class:ordered={order !== null} class:marked class:blocked={!choosable} disabled={!choosable} aria-pressed={marked} onclick={onselect}>
			{@render pickerBody()}
		</button>
	{:else}
		<div class="account-usage-row picker" class:ordered={order !== null} class:marked class:blocked={!choosable} aria-current={marked ? 'true' : undefined}>
			{@render pickerBody()}
		</div>
	{/if}
{:else}
	<div class="account-usage-row" class:compact class:ordered={order !== null}>
		{#if order !== null}<span class="order" aria-label={`Rollover order ${order}`}>{order}</span>{/if}
		<strong><span class="name">{label.name}</span>{#if marker}<span class="separator" aria-hidden="true">{' · '}</span><span class="marker">{marker}</span>{/if}{#if hasEmail}<span class="separator" aria-hidden="true">{' · '}</span><span class="email">{label.email}</span>{/if}</strong>
		{#if usageLimit}
			<!-- A limited account cannot run a turn: red, as the picker shows it. -->
			<p class="usage" aria-label="Account usage"><span class="red">{usageLimit}</span></p>
		{:else if segments.length}
			<p class="usage" aria-label={`Account usage: ${usageLabel}`}>
				{#each segments as segment, index (index)}
					{#if index > 0}<span aria-hidden="true">{' · '}</span>{/if}
					<span class:amber={segment.level === 'amber'} class:red={segment.level === 'red'}>{segment.text}</span>
				{/each}
			</p>
		{/if}
		{#if checking}<p class="usage" aria-label="Account usage check">Checking account…</p>{/if}
		{#if checkError}<p class="usage" aria-label="Account usage check">{checkError}</p>{/if}
		{#if children}<span class="controls">{@render children()}</span>{/if}
	</div>
{/if}

<style>
	.account-usage-row { min-width: 0; }
	strong { display: block; color: var(--text-primary); font-size: var(--text-subhead); overflow-wrap: anywhere; }
	.email { color: var(--text-secondary); font-weight: 400; }
	.marker { margin-left: var(--space-1); color: var(--text-secondary); font-size: var(--text-caption); font-weight: 400; }
	.usage { margin: 3px 0 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.usage .amber { color: var(--amber-text); }
	.usage .red { color: var(--red-text); }
	/* Compact is a Settings list row: row-name type, usage as trailing detail, space alone between rows. */
	.compact { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; column-gap: var(--space-2); row-gap: var(--space-1); min-width: 0; min-height: var(--list-row-min-height); }
	.compact.ordered { grid-template-columns: var(--space-3) minmax(0, 1fr) auto auto; }
	.compact strong { grid-column: 1; min-width: 0; font-size: var(--text-subhead); font-weight: 400; }
	.compact.ordered > strong { grid-column: 2; }
	.compact .usage { grid-column: 2; margin: 0; text-align: right; white-space: nowrap; font-size: var(--text-caption); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
	.compact.ordered > .usage { grid-column: 3; }
	.order { grid-column: 1; color: var(--text-secondary); font-size: var(--text-footnote); font-variant-numeric: tabular-nums; text-align: center; }
	/* The shared control and status are separate grid participants, so status never widens controls. */
	.compact .controls, .compact :global(.provider-account-policy-control) { display: contents; }
	.compact :global(.provider-account-policy-controls) { grid-column: -2 / -1; grid-row: 1; justify-self: end; }
	.compact :global(.policy-status) { grid-column: 1 / -1; justify-self: end; text-align: right; }
	/* A row action (Sign in) takes the usage column on the name line, ahead of the policy controls; a long name truncates beside it. */
	.compact :global(.link-target .btn),
	.compact :global(.link-target .account-signin-action) { grid-row: 1; grid-column: -3 / -2; justify-self: end; }
	.compact:has(:global(.link-target)) strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	/* Picker: one compact line under the vendor's group header, set in with the model rows. */
	.account-usage-row.picker {
		position: relative;
		--check-top: calc(6px + 0.5lh); /* the first line's centre: the figures may wrap to a second line */
		display: flex; align-items: baseline; gap: 8px;
		width: 100%; box-sizing: border-box;
		padding: 6px 10px 6px 32px;
		font-family: inherit; font-size: var(--text-footnote);
		color: var(--text-primary); text-align: left;
		background: none; border: none; border-radius: var(--radius-xs);
	}
	button.account-usage-row.picker { cursor: pointer; }
	button.account-usage-row.picker:hover:not(:disabled):not(.marked) { background: var(--bg-hover); }
	button.account-usage-row.picker:disabled { cursor: default; }
	.account-usage-row.picker.blocked { color: var(--text-secondary); }
	/* The selected account only gains the fill, as the model row does: its content stays where an unselected row's is, and its check enters the left gutter. */
	.account-usage-row.picker.marked { background: color-mix(in srgb, var(--accent) 10%, transparent); }
	/* Rollover rows use the model-dot gutter for their order and the model-row gap before the account label. */
	.account-usage-row.picker.ordered { padding-left: 32px; gap: 3px; }
	.picker.ordered .order { flex: 0 0 14px; margin: 0; text-align: center; color: var(--text-tertiary); font-size: var(--text-caption); font-weight: 600; }
	.main { flex: 1 1 0; min-width: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
	/* A long account name wraps in its row; an email yields with an ellipsis. */
	.picker-label { flex: 1 1 140px; min-width: 0; display: flex; flex-wrap: wrap; align-items: baseline; overflow: hidden; }
	.picker-label .name { flex: 0 1 auto; min-width: 0; font-weight: 600; color: var(--text-primary); overflow-wrap: anywhere; }
	.picker-label .sep { flex: 0 0 auto; padding: 0 3px; color: var(--text-secondary); white-space: pre; }
	.picker-label .email { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.figures { margin-left: auto; text-align: right; color: var(--text-tertiary); font-variant-numeric: tabular-nums; white-space: nowrap; }
	.figures .amber { color: var(--amber-text); }
	.figures .red { color: var(--red-text); }
	/* The pending icon follows the name of the account being switched to. */
	.name-group { flex: 0 1 auto; min-width: 0; display: flex; align-items: baseline; gap: var(--space-1); }
	/* A block with its own line box, so the icon's vertical-align sits it on the text baseline; an empty flex box would rest its bottom there. */
	.switching-slot { display: inline-block; flex: 0 0 auto; }
	@media (max-width: 480px) {
		.figures { flex-basis: 100%; margin-left: 0; text-align: left; }
		/* Phone: name and usage stack in the middle column; order and controls stay beside them. */
		.compact { grid-template-columns: minmax(0, 1fr) auto; column-gap: var(--space-2); row-gap: var(--space-1); }
		/* Match the ordered detail indent to this fixed gutter, rather than a numeral's glyph width. */
		.compact.ordered { grid-template-columns: var(--space-3) minmax(0, 1fr) auto; }
		.compact > strong, .compact > .usage { grid-column: -3 / -2; }
		.compact .usage { margin: 0; text-align: left; white-space: normal; }
		.compact > .order { grid-column: 1; grid-row: 1 / span 2; align-self: start; }
		.compact :global(.provider-account-policy-controls) { grid-column: -2 / -1; grid-row: 1 / span 2; }
		.compact :global(.policy-status) { grid-column: -3 / -1; justify-self: end; }
		.compact strong { display: flex; flex-wrap: wrap; align-items: baseline; }
		.compact strong .email { flex-basis: 100%; }
		.compact strong .separator { display: none; }
		.compact strong .marker { flex: 0 0 auto; }
		/* The action takes the controls column; with policy controls too, a column of its own beside them. */
		.compact :global(.link-target .btn),
		.compact :global(.link-target .account-signin-action) { grid-column: -2 / -1; }
		.compact:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) { grid-template-columns: minmax(0, 1fr) auto auto; }
		.compact.ordered:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) { grid-template-columns: var(--space-3) minmax(0, 1fr) auto auto; }
		.compact:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) > strong,
		.compact:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) > .usage { grid-column: -4 / -3; }
		.compact:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) :global(.link-target .btn),
		.compact:has(:global(.link-target)):has(:global(.provider-account-policy-controls)) :global(.link-target .account-signin-action) { grid-column: -3 / -2; }
		/* The 44px action makes the name line taller; the rank centres on that line, as the name does, instead of staying at the top. */
		.compact:has(:global(.link-target)) > .order { grid-row: 1; align-self: center; }
		.compact:has(:global(.link-target)) strong .name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	}
</style>
