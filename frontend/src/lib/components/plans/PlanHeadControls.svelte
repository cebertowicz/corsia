<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import { connectionState } from '$lib/stores/connection';
	import { token } from '$lib/stores/auth';
	import { mutatePlanLifecycle } from '$lib/api/plans';
	import { errorText } from '$lib/api/planRead';

	/**
	 * The plan's count of unfinished work and its Pause/Resume, for whatever
	 * header the host owns: the desktop section cap shows both, the phone
	 * drawer puts Pause alone at its leading edge and the count by its title.
	 */
	let { threadId, show = 'both' }: { threadId: string; show?: 'both' | 'pause' } = $props();

	const query = planQuery(() => ({ kind: 'thread', id: threadId }));
	const plan = $derived(query.payload?.plans[0] ?? null);
	const offline = $derived($connectionState !== 'connected' && query.payload != null);
	const open = $derived(plan?.progress.open ?? 0);
	const operation = $derived(plan?.plan.state === 'paused' ? 'resume' : 'pause');

	let busy = $state(false);
	let error = $state<string | null>(null);
	const canMutate = $derived(!!plan && !!$token && query.status === 'ready' && !offline && !busy);

	async function changeLifecycle(): Promise<void> {
		const current = plan;
		const bearer = $token;
		if (!current || !bearer || !canMutate) return;
		const op = operation;
		const revision = current.plan.revision;
		busy = true;
		error = null;
		const result = await mutatePlanLifecycle(bearer, current.plan.id, op, revision, `${op}:${current.plan.id}:${revision}`);
		busy = false;
		if (result.status === 'success') {
			query.retry();
			return;
		}
		if (result.status === 'conflict') {
			error = 'This plan changed. Refreshing the latest state.';
			query.retry();
			return;
		}
		error = result.status === 'forbidden' ? 'You no longer have permission to change this plan.' : errorText(result);
	}
</script>

{#if plan}
	<span class="plan-head-controls">
		{#if show === 'both' && open > 0}
			<!-- One count of unfinished work; the agent-read budget is a hint on it, never a second number. -->
			<span
				class="progress"
				class:oversize={!!plan.oversize}
				title={plan.oversize ? `This plan has more than ${plan.oversize.budget} open tasks. Use Mark done or Won’t do on any that don’t need doing.` : undefined}
			>{open} left</span>
		{/if}
		{#if error}<span class="lifecycle-error" role="alert" title={error}>{error}</span>{/if}
		<Button
			variant="ghost"
			size="sm"
			disabled={!canMutate}
			loading={busy}
			title={operation === 'pause' ? 'Pause future plan continuation' : 'Resume plan continuation'}
			onclick={changeLifecycle}
		>
			{operation === 'pause' ? 'Pause' : 'Resume'}
		</Button>
	</span>
{/if}

<style>
	.plan-head-controls { display: inline-flex; min-width: 0; align-items: center; gap: var(--space-1); }
	/* Pause reads as the plan's other trailing actions (Open, Show 3): caption, tertiary, primary on hover. */
	.plan-head-controls :global(.btn.ghost) { padding: 0 2px 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-regular, 400); }
	@media (hover: hover) { .plan-head-controls :global(.btn.ghost:hover:not(:disabled)) { color: var(--text-primary); } }
	.progress { color: var(--text-tertiary); font-size: var(--text-caption); font-variant-numeric: tabular-nums; white-space: nowrap; }
	.progress.oversize { text-decoration: underline dotted; cursor: help; }
	.lifecycle-error {
		min-width: 0; overflow: hidden; color: var(--red); font-size: var(--text-caption);
		text-overflow: ellipsis; white-space: nowrap;
	}
</style>
