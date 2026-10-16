<script lang="ts">
	import { Avatar, AvatarStack, Popover } from '$lib/components/ui';
	import PresenceShowcase from './PresenceShowcase.svelte';
	let trigger = $state<HTMLElement>();
	let open = $state(false);
</script>

<section class="block">
	<header class="block-head"><h2>Comment primitives</h2><p>Anchored comment detail and reusable participant identity.</p></header>
	<div class="card stack">
		<div class="row"><Avatar name="Quality Gate" color="var(--purple)" /><span>Named avatar</span><Avatar name="Bugs" color="var(--amber)" hidden /><span>Adjacent-name avatar</span></div>
		<AvatarStack people={[{ id: 'quality-gate', name: 'Quality Gate', color: 'var(--purple)' }, { id: 'bugs', name: 'Bugs', color: 'var(--amber)' }, { id: 'context-memory', name: 'Context & Memory', color: 'var(--accent)' }, { id: 'structured-plans', name: 'Structured Plans', color: 'var(--green)' }]} />
		<button bind:this={trigger} onclick={() => (open = !open)}>Show popover</button>
	</div>
	{#snippet content()}<p class="popover-copy">A non-modal anchored comment surface.</p>{/snippet}
	<Popover anchor={trigger} {open} onClose={() => (open = false)}>{@render content()}</Popover>
</section>
<PresenceShowcase />

<style>
	.row, .stack { display: flex; align-items: center; gap: 8px; }
	.card { flex-wrap: wrap; }
	button { padding: 6px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); color: var(--text-primary); font: inherit; cursor: pointer; }
	.popover-copy { margin: 0; color: var(--text-secondary); }
</style>
