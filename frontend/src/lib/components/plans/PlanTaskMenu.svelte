<script lang="ts">
	import { onMount } from 'svelte';
	import type { OperatorNode, OperatorUserAction } from '$lib/api/plans';
	import { portal } from '$lib/actions/portal';

	let {
		node,
		x,
		y,
		onAction,
		onClose,
	}: {
		node: OperatorNode;
		x: number;
		y: number;
		/** Resolves to the server's refusal text, or null once the action landed. */
		onAction: (action: OperatorUserAction) => Promise<string | null> | void;
		onClose: () => void;
	} = $props();

	const LABELS: Record<OperatorUserAction, string> = {
		mark_done: 'Mark done',
		pause_node: 'Pause',
		resume_node: 'Resume',
		close_node: 'Won’t do',
	};
	const MENU_W = 220;
	const MENU_H = 168;

	let busy = $state(false);
	let error = $state<string | null>(null);
	let menuEl = $state<HTMLElement | undefined>(undefined);

	const left = $derived(Math.max(8, Math.min(x, window.innerWidth - MENU_W - 8)));
	const top = $derived(Math.max(8, Math.min(y, window.innerHeight - MENU_H - 8)));
	const stopsWorker = $derived(
		node.child?.available === true && node.user_actions.some((a) => a === 'close_node' || a === 'mark_done'),
	);

	onMount(focusFirst);

	function items(): HTMLButtonElement[] {
		return [...(menuEl?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];
	}

	// The menu is `position: fixed` and portaled to `document.body`, so it is
	// already in view where it opens. Every `.focus()` passes `preventScroll`
	// so moving focus never scrolls the page or the pane under it.
	function focusFirst() {
		items()[0]?.focus({ preventScroll: true });
	}

	/** Every action, Won't do included, is one tap: no prompt stands in front of it. */
	async function run(action: OperatorUserAction) {
		if (busy) return;
		busy = true;
		error = null;
		const failure = await onAction(action);
		busy = false;
		if (failure) error = failure;
		else onClose();
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.stopPropagation();
			onClose();
			return;
		}
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
		event.preventDefault();
		const all = items();
		const at = all.indexOf(document.activeElement as HTMLButtonElement);
		const step = event.key === 'ArrowDown' ? 1 : -1;
		all[(at + step + all.length) % all.length]?.focus({ preventScroll: true });
	}
</script>

<!--
	The menu is `position: fixed`, but any ancestor establishing a CSS
	containing block (a `backdrop-filter` pane, a transformed sheet) makes
	that `fixed` resolve against the ancestor's box instead of the
	viewport. Portaling both roots directly to `document.body` sidesteps
	that (a wrapping element here would just become the new containing
	ancestor for the menu instead).
-->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="task-menu-backdrop" onclick={onClose} use:portal></div>
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={menuEl}
	class="task-menu"
	role="menu"
	tabindex="-1"
	aria-label={`Actions for ${node.title}`}
	style={`left: ${left}px; top: ${top}px;`}
	onkeydown={onKeydown}
	use:portal
>
	{#each node.user_actions as action (action)}
		<button
			class="press-tint task-menu-item"
			class:danger={action === 'close_node'}
			type="button"
			role="menuitem"
			disabled={busy}
			onclick={() => run(action)}
		>{LABELS[action]}</button>
	{/each}
	{#if stopsWorker}<p class="note">Mark done or Won’t do stops its worker too.</p>{/if}
	{#if error}
		<p class="menu-error" role="alert">{error}</p>
	{/if}
</div>

<style>
	.task-menu-backdrop { position: fixed; inset: 0; z-index: 200; }
	.task-menu {
		position: fixed; z-index: 201; box-sizing: border-box; width: 220px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
		padding: 4px;
	}
	.task-menu-item {
		display: flex; align-items: center; gap: 8px;
		width: 100%; padding: 7px 10px;
		background: none; border: none; border-radius: var(--radius-2xs);
		color: var(--text-primary); font-family: inherit; font-size: var(--text-subhead);
		cursor: pointer; text-align: left;
		transition: background var(--dur-tight);
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.task-menu-item:hover:not(:disabled) { background: var(--bg-hover); }
	}
	.task-menu-item:focus-visible { background: var(--bg-hover); }
	.task-menu-item:disabled { opacity: 0.6; cursor: default; }
	.task-menu-item.danger { color: var(--red); }
	.note { margin: 0; padding: 2px 10px 4px; color: var(--text-secondary); font-size: var(--text-caption); }
	.menu-error { margin: 0; padding: 6px 10px; color: var(--red); font-size: var(--text-caption); }
	@media (max-width: 768px) {
		/* The menu is the phone's only lifecycle-action surface; every action keeps the HIG touch floor. */
		.task-menu-item { min-height: 44px; padding-block: var(--space-3); }
	}
</style>
