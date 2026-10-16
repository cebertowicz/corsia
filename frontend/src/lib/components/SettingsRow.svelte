<script lang="ts">
	import type { Snippet } from 'svelte';
	import Menu, { type MenuItem } from './ui/Menu.svelte';
	import { createRowMenu } from './ui/rowMenu.svelte';

	// A Settings list row in the DESIGN-SYSTEM §0.1 list-row role: flat on the
	// pane, a second line, a trailing detail, and verbs on right-click or long-press.
	let {
		name,
		sub = '',
		subTone = 'quiet',
		trailing = '',
		chevron = false,
		mark = null,
		menuItems = [],
		onOpen,
		onMenuSelect,
		actions,
	}: {
		name: string;
		sub?: string;
		/** A second line stating a failure takes the status's text colour. */
		subTone?: 'quiet' | 'failed';
		trailing?: string;
		chevron?: boolean;
		/** A project's colour, drawn as its §0.1 project mark before the name. */
		mark?: string | null;
		menuItems?: MenuItem[];
		onOpen?: () => void;
		onMenuSelect?: (id: string) => void;
		/** Trailing text actions, for a verb that must stay visible. */
		actions?: Snippet;
	} = $props();

	const menu = createRowMenu<string>(() => menuItems.length > 0 && !!onMenuSelect);

	function select(id: string) {
		menu.close();
		onMenuSelect?.(id);
	}
</script>

{#snippet content()}
	{#if mark}<span class="mark" style:background={mark} aria-hidden="true"></span>{/if}
	<span class="copy">
		<span class="name">{name}</span>
		{#if sub}<span class="sub" class:failed={subTone === 'failed'}>{sub}</span>{/if}
	</span>
	{#if trailing}<span class="trailing">{trailing}</span>{/if}
	{#if chevron}
		<svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
	{/if}
{/snippet}

<div class="settings-row" data-settings-row={name} {...menu.rowHandlers(name)}>
	{#if onOpen}
		<button type="button" class="face open" onclick={onOpen}>{@render content()}</button>
	{:else}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div class="face" tabindex={menuItems.length ? 0 : undefined}>{@render content()}</div>
	{/if}
	{#if actions}<span class="actions">{@render actions()}</span>{/if}
</div>
{#if menu.open}
	<Menu ariaLabel={`Actions for ${name}`} items={menuItems} x={menu.open.x} y={menu.open.y} onSelect={select} onClose={menu.close} />
{/if}

<style>
	.settings-row {
		display: flex; align-items: center; gap: var(--space-2);
		min-height: var(--list-row-min-height);
		border-radius: var(--radius-sm);
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last row tapped. */
	@media (hover: hover) {
		.settings-row:hover { background: var(--bg-hover); }
	}
	.face {
		flex: 1; min-width: 0;
		display: flex; align-items: center; gap: var(--space-3);
		min-height: var(--list-row-min-height);
		padding: var(--space-1) var(--space-2);
		background: none; border: 0; border-radius: var(--radius-sm);
		font: inherit; color: inherit; text-align: left;
	}
	.face.open { cursor: pointer; }
	.face:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.mark {
		flex-shrink: 0;
		width: var(--project-mark-size); height: var(--project-mark-size);
		border-radius: var(--radius-circle);
		box-shadow: var(--project-mark-ring);
	}
	.copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); }
	.name, .sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.name { font-size: var(--text-subhead); color: var(--text-primary); }
	.sub { font-size: var(--text-caption); color: var(--text-tertiary); }
	.sub.failed { color: var(--red-text); }
	.trailing { flex-shrink: 0; font-size: var(--text-caption); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
	.chev { flex-shrink: 0; color: var(--text-tertiary); }
	.actions { flex-shrink: 0; display: flex; align-items: center; gap: var(--space-1); padding-right: var(--space-1); }
</style>
