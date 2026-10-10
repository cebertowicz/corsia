<script lang="ts">
	import type { UserActionHandler } from '$lib/api/plans';
	import PlanTaskMenu from './PlanTaskMenu.svelte';
	import type { createTaskMenu } from './taskMenu.svelte';

	let { menu, onUserAction }: {
		menu: ReturnType<typeof createTaskMenu>;
		onUserAction?: UserActionHandler;
	} = $props();
</script>

{#if menu.open && onUserAction}
	{@const target = menu.open}
	<PlanTaskMenu
		node={target.node}
		x={target.x}
		y={target.y}
		onAction={(action) => onUserAction(target.node, action)}
		onClose={menu.close}
	/>
{/if}
