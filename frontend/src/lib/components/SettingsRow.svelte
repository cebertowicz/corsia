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
		expanded,
		stackActions = false,
		mark = null,
		menuItems = [],
		onOpen,
		onMenuSelect,
		actions,
	}: {
		name: string;
		/** The second line. A snippet draws a two-tone line: it takes the tone's colour, and a part of it can set its own. */
		sub?: string | Snippet;
		/** A second line stating a status takes that status's text colour (Needs you is `attention`). */
		subTone?: 'quiet' | 'failed' | 'attention';
		trailing?: string;
		chevron?: boolean;
		/** An opening row reports whether its details are showing; the chevron turns down with them. */
		expanded?: boolean;
		/** The actions drop below the copy when the row is too narrow for both, so a long second line is not cut to make room for them. */
		stackActions?: boolean;
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
		{#if sub}<span class="sub" class:multi={typeof sub !== 'string'} class:failed={subTone === 'failed'} class:attention={subTone === 'attention'}>{#if typeof sub === 'string'}{sub}{:else}{@render sub()}{/if}</span>{/if}
	</span>
	{#if trailing}<span class="trailing">{trailing}</span>{/if}
{/snippet}

<div class="settings-row" class:stack={stackActions} data-settings-row={name} {...menu.rowHandlers(name)}>
	{#if onOpen}
		<button type="button" class="face open" aria-expanded={expanded} onclick={onOpen}>{@render content()}</button>
	{:else}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div class="face" tabindex={menuItems.length ? 0 : undefined}>{@render content()}</div>
	{/if}
	{#if actions}<span class="actions">{@render actions()}</span>{/if}
	{#if chevron}
		<svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
	{/if}
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
	/* A drawn second line carries a status and a place; it wraps rather than hiding either. */
	.sub.multi { white-space: normal; overflow-wrap: anywhere; }
	.sub.failed { color: var(--red-text); }
	.sub.attention { color: var(--amber-text); }
	.trailing { flex-shrink: 0; font-size: var(--text-caption); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
	.chev { flex-shrink: 0; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.settings-row:has(.face[aria-expanded='true']) .chev { transform: rotate(90deg); }
	@media (prefers-reduced-motion: reduce) { .chev { transition: none; } }
	/* Preserve the ghost button's symmetric hit area while its fill reaches the row edge. */
	/* The actions drop below the copy only when the row is too narrow for both, so the second line is never cut to make room. */
	.settings-row.stack { flex-wrap: wrap; }
	.stack .face { flex: 1 1 16rem; }
	.stack .actions { margin-left: auto; }
	.actions { flex-shrink: 0; display: flex; align-items: center; gap: var(--space-1); padding-right: var(--space-1); margin-right: calc(-1 * var(--button-pad-x-sm)); }
</style>
