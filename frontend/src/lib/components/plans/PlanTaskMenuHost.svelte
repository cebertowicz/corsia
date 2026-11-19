<script lang="ts">
	import type { OperatorNode, UserActionHandler } from '$lib/api/plans';
	import PlanTaskMenu from './PlanTaskMenu.svelte';
	import type { createTaskMenu } from './taskMenu.svelte';

	let { menu, onUserAction, onMove, moveAvailability }: {
		menu: ReturnType<typeof createTaskMenu>;
		onUserAction?: UserActionHandler;
		onMove: (node: OperatorNode, direction: 'up' | 'down' | 'top') => Promise<string | null>;
		moveAvailability: (node: OperatorNode) => { eligible: boolean; up: boolean; down: boolean };
	} = $props();
</script>

{#if menu.open && onUserAction}
	{@const target = menu.open}
	<PlanTaskMenu
		node={target.node}
		x={target.x}
		y={target.y}
		onAction={(action) => onUserAction(target.node, action)}
		onMove={(direction) => onMove(target.node, direction)}
		canReorder={moveAvailability(target.node).eligible}
		canMoveUp={moveAvailability(target.node).up}
		canMoveDown={moveAvailability(target.node).down}
		onClose={menu.close}
	/>
{/if}
