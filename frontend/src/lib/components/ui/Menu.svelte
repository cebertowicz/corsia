<script lang="ts" module>
	export interface MenuItem {
		id: string;
		label: string;
		/** Destructive or irreversible: drawn in red. */
		danger?: boolean;
		disabled?: boolean;
		/** What the verb does here, when its label alone can't say (a pointer's tooltip). */
		title?: string;
	}
</script>

<script lang="ts">
	import { onMount } from 'svelte';
	import { portal } from '$lib/actions/portal';
	import { menuPop } from '$lib/popoverMotion';

	/**
	 * The one row-actions menu: a short list of verbs opened at a point (a
	 * right-click, Shift+F10, or a long-press). A row owns its verbs
	 * and what they do; this owns placement, focus, keys and the touch floor.
	 * Sidebar thread rows still use SidebarContextMenu until #857.
	 */
	let {
		ariaLabel,
		items,
		x,
		y,
		busy = false,
		note = null,
		error = null,
		onSelect,
		onClose,
	}: {
		ariaLabel: string;
		items: MenuItem[];
		x: number;
		y: number;
		/** An action is in flight: every item waits. */
		busy?: boolean;
		/** One quiet line under the items, such as a side effect of an action. */
		note?: string | null;
		error?: string | null;
		onSelect: (id: string) => void;
		onClose: () => void;
	} = $props();

	const MENU_W = 220;
	const ITEM_H = 44;

	let menuEl = $state<HTMLElement | undefined>(undefined);

	// The note and error lines add height the item count cannot see, so the clamp uses the
	// rendered height once there is one and the item estimate only before first layout.
	let menuH = $state(0);
	$effect(() => {
		void [items.length, note, error];
		if (menuEl) menuH = menuEl.offsetHeight;
	});

	const left = $derived(Math.max(8, Math.min(x, window.innerWidth - MENU_W - 8)));
	const top = $derived(
		Math.max(8, Math.min(y, window.innerHeight - (menuH ? menuH + 8 : items.length * ITEM_H + 16)))
	);

	onMount(() => entries()[0]?.focus({ preventScroll: true }));

	function entries(): HTMLButtonElement[] {
		return [...(menuEl?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];
	}

	// The menu is already in view where it opens: no focus move may scroll the pane under it.
	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.stopPropagation();
			onClose();
			return;
		}
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
		event.preventDefault();
		const all = entries();
		const at = all.indexOf(document.activeElement as HTMLButtonElement);
		const step = event.key === 'ArrowDown' ? 1 : -1;
		all[(at + step + all.length) % all.length]?.focus({ preventScroll: true });
	}
</script>

<!--
	`position: fixed` resolves against any ancestor that makes a containing
	block (a `backdrop-filter` pane, a transformed sheet), so both roots are
	portaled straight to `document.body`; a wrapper would only become that
	ancestor again.
-->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="menu-backdrop" onclick={onClose} use:portal></div>
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={menuEl}
	class="menu"
	role="menu"
	tabindex="-1"
	aria-label={ariaLabel}
	style={`left: ${left}px; top: ${top}px;`}
	onkeydown={onKeydown}
	use:portal
	in:menuPop|global={{ origin: 'top left' }}
	out:menuPop|global={{ origin: 'top left' }}
>
	{#each items as item (item.id)}
		<button
			class="press-tint menu-item"
			class:danger={item.danger}
			type="button"
			role="menuitem"
			disabled={busy || item.disabled}
			title={item.title}
			onclick={() => onSelect(item.id)}
		>{item.label}</button>
	{/each}
	{#if note}<p class="note">{note}</p>{/if}
	{#if error}<p class="menu-error" role="alert">{error}</p>{/if}
</div>

<style>
	.menu-backdrop { position: fixed; inset: 0; z-index: 200; }
	.menu {
		position: fixed; z-index: 201; box-sizing: border-box; width: 220px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		box-shadow: var(--glass-elevation-2);
		padding: var(--space-1);
	}
	.menu-item {
		display: flex; align-items: center; gap: var(--space-2);
		width: 100%; padding: 7px 10px;
		background: none; border: none; border-radius: var(--radius-2xs);
		color: var(--text-primary); font-family: inherit; font-size: var(--text-subhead);
		cursor: pointer; text-align: left;
		transition: background var(--dur-tight);
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.menu-item:hover:not(:disabled) { background: var(--bg-hover); }
	}
	.menu-item:focus-visible { background: var(--bg-hover); }
	.menu-item:disabled { opacity: 0.6; cursor: default; }
	.menu-item.danger { color: var(--red); }
	.note { margin: 0; padding: 2px 10px 4px; color: var(--text-secondary); font-size: var(--text-caption); }
	.menu-error { margin: 0; padding: 6px 10px; color: var(--red); font-size: var(--text-caption); }
	@media (max-width: 768px) {
		/* On a phone the menu may be a row's only action surface: every item keeps the HIG touch floor. */
		.menu-item { min-height: 44px; padding-block: var(--space-3); }
	}
</style>
