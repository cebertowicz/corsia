<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { drainStatus } from '$lib/stores/connection';
	import { toasts } from '$lib/stores/toast';
	import { changeList, costLine, formatScheduleTime, releaseNotesFor } from '$lib/update/model';
	import { saveUpdateSchedule, startUpdate, updateOperation, updateStarting, updateStatus } from '$lib/update/store';
	import SettingsUpdateChanges from './SettingsUpdateChanges.svelte';

	/** The update on offer: what changed, what it costs, and when to take it. */
	interface Props {
		saving: boolean;
		saveSchedule: (patch: { tonight?: boolean }) => Promise<boolean>;
	}

	const { saving, saveSchedule }: Props = $props();

	const notes = $derived(releaseNotesFor($updateOperation, $updateStatus));
	const list = $derived(changeList(notes?.changes));
	const total = $derived((notes?.changes?.items.length ?? 0) + (notes?.changes?.more ?? 0));
	const agentCount = $derived($drainStatus?.activeTurns ?? $updateStatus?.agents_working ?? 0);
	const schedule = $derived($updateStatus?.schedule);
	const scheduledTonight = $derived(Boolean(schedule?.tonight_at));

	async function tonight(): Promise<void> {
		if (!await saveSchedule({ tonight: true })) return;
		toasts.showAction(`Update scheduled for ${formatScheduleTime(schedule?.time ?? '')} tonight.`, {
			label: 'Undo', run: () => { void saveUpdateSchedule({ tonight: false }); },
		});
	}
</script>

<div class="offer">
	{#if list}<SettingsUpdateChanges heading={list.heading} items={list.items} {total} />{/if}
	<p class="cost">{costLine($updateStatus?.usual_seconds, agentCount)}</p>
	<div class="actions">
		<Button variant="primary" size="sm" loading={$updateStarting} onclick={() => void startUpdate()}>Update now</Button>
		{#if scheduledTonight}
			<Button variant="ghost" size="sm" disabled={saving} onclick={() => void saveSchedule({ tonight: false })}>Cancel tonight's update</Button>
		{:else if schedule && !schedule.automatic}
			<Button variant="ghost" size="sm" disabled={saving} onclick={() => void tonight()}>Tonight at {formatScheduleTime(schedule.time)}</Button>
		{/if}
	</div>
</div>

<style>
	/* Read content under the Version row, flat on the pane at the row inset (§0.1). */
	.offer { display: grid; gap: var(--space-3); padding: var(--space-1) var(--space-2) var(--space-2); color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.5; }
	.cost { margin: 0; }
	/* Natural width at every size: a full-width ghost reads as a line of text, not a button. */
	.actions { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
</style>
