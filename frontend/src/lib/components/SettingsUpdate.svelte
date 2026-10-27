<script lang="ts">
	import { onDestroy } from 'svelte';
	import { Spinner, Toggle } from '$lib/components/ui';
	import { saveUpdateSchedule, updateFailureVerdict, updateInFlight, updateOperation, updateServerClockOffsetMs, updateStatus, updateUnreachable } from '$lib/update/store';
	import { failureCopy, failureFromOperation, failureFromVerdict } from '$lib/update/failureCopy';
	import { changeList, currentOperation, elapsedSeconds, formatElapsed, formatInstalledAt, formatScheduleTime, isFailedOperation, releaseNotesFor, stepMessage } from '$lib/update/model';
	import SettingsUpdateChanges from './SettingsUpdateChanges.svelte';
	import SettingsUpdateLast from './SettingsUpdateLast.svelte';
	import SettingsUpdateOffer from './SettingsUpdateOffer.svelte';

	let now = $state(Date.now());
	const tick = setInterval(() => { now = Date.now(); }, 1_000);
	onDestroy(() => clearInterval(tick));

	const operation = $derived(currentOperation($updateOperation, $updateStatus));
	const running = $derived($updateInFlight);
	// A verdict the client reached itself wins over whatever the latest operation says.
	const failure = $derived($updateFailureVerdict ? failureFromVerdict($updateFailureVerdict) : isFailedOperation(operation) ? failureFromOperation(operation) : null);
	const copy = $derived(failure ? failureCopy(failure) : null);
	const completedVersion = $derived(operation?.status === 'complete' ? operation.target_version : null);
	// A same-day development build is told apart by time; the update that installed it carries that longer form.
	const currentVersion = $derived(
		completedVersion && $updateStatus?.current_version && completedVersion.startsWith($updateStatus.current_version)
			? completedVersion : ($updateStatus?.current_version ?? 'Unknown'),
	);
	const targetVersion = $derived($updateStatus?.target_version ?? operation?.target_version);
	const offered = $derived(!running && !copy && Boolean($updateStatus?.available));
	const installedAt = $derived(operation?.status === 'complete' && operation.settled_at ? formatInstalledAt(operation.settled_at, now) : null);
	const overnight = $derived(operation?.trigger === 'tonight' || operation?.trigger === 'automatic');
	const doneChanges = $derived(operation?.status === 'complete' ? (releaseNotesFor(operation, $updateStatus)?.changes ?? null) : null);
	const doneNotes = $derived(changeList(doneChanges));
	const doneTotal = $derived((doneChanges?.items.length ?? 0) + (doneChanges?.more ?? 0));
	const installedLine = $derived.by(() => {
		if (!installedAt) return null;
		if (!overnight) return `Installed ${installedAt}`;
		return installedAt.startsWith('today ') ? `Updated overnight ${installedAt.slice(6)}` : `Updated overnight, ${installedAt}`;
	});
	const elapsed = $derived(formatElapsed(elapsedSeconds(operation, now, $updateServerClockOffsetMs)));
	const schedule = $derived($updateStatus?.schedule);
	const scheduledTonight = $derived(Boolean(schedule?.tonight_at));
	let scheduleSaving = $state(false);
	async function saveSchedule(patch: { automatic?: boolean; time?: string; tonight?: boolean }): Promise<boolean> {
		scheduleSaving = true;
		try {
			return await saveUpdateSchedule(patch);
		} finally {
			scheduleSaving = false;
		}
	}
</script>

<div class="update-card">
	<div class="service-row">
		<span class="service-name">Version</span>
		<div class="version-state">
			<span class="version-line">
				<span class="service-version">{currentVersion}</span>
				{#if scheduledTonight}<span class="state offer-state">· {formatScheduleTime(schedule?.time ?? '')} tonight</span>
				{:else if copy}<span class="state failed-state">· {copy.title}</span>
				{:else if offered && targetVersion}<span class="state offer-state">· {targetVersion} available</span>{/if}
				{#if running}<span class="updating">· <Spinner size={12} />{stepMessage(operation, $updateUnreachable)}<span class="time">{elapsed}</span></span>{/if}
			</span>
			{#if installedLine}<span class="installed">{installedLine}</span>{/if}
		</div>
	</div>

	{#if offered}<SettingsUpdateOffer saving={scheduleSaving} {saveSchedule} />{/if}

	{#if doneNotes}
		<div class="notes"><SettingsUpdateChanges heading={completedVersion ? `${doneNotes.heading} in ${completedVersion}` : doneNotes.heading} items={doneNotes.items} total={doneTotal} /></div>
	{/if}

	{#if copy && failure}<SettingsUpdateLast {copy} {failure} />{/if}

	<div class="automatic-row">
		<div class="automatic-copy"><span>Keep CORSIA up to date automatically</span>{#if schedule?.automatic}<span class="service-status">Installs at {formatScheduleTime(schedule?.time ?? '')}.</span>{/if}</div>
		<div class="service-actions">
			{#if schedule?.automatic}<input aria-label="Automatic update time" class="time-field" type="time" value={schedule?.time ?? ''} disabled={scheduleSaving} onchange={(event) => void saveSchedule({ time: event.currentTarget.value })} />{/if}
			<Toggle checked={schedule?.automatic ?? false} onchange={(next) => void saveSchedule({ automatic: next })} ariaLabel="Automatically install updates overnight" disabled={scheduleSaving} />
		</div>
	</div>
</div>

<style>
	/* Rows take the §0.1 list-row and second-line treatment (as SettingsRow draws them). The Version row
	   carries a spinner and a status in its own colour, which SettingsRow's plain-text line cannot. */
	.update-card { display: flex; flex-direction: column; gap: var(--space-1); }
	.service-row { display: flex; align-items: center; gap: var(--space-3); min-height: var(--list-row-min-height); padding: var(--space-1) var(--space-2); }
	.service-name { font-size: var(--text-subhead); color: var(--text-primary); min-width: 70px; }
	/* The version and its state, an update in progress included, are the row's one-line trailing detail. */
	.version-state { display: grid; justify-items: end; gap: var(--space-1); min-width: 0; margin-left: auto; text-align: right; }
	.version-line { display: inline-flex; align-items: center; justify-content: flex-end; gap: var(--space-1); min-width: 0; flex-wrap: wrap; font-size: var(--text-caption); }
	.installed { color: var(--text-tertiary); font-size: var(--text-caption); }
	.service-version { color: var(--text-tertiary); overflow-wrap: anywhere; }
	.offer-state { color: var(--accent-text); }
	.failed-state { color: var(--amber-text); }
	.service-status { color: var(--text-tertiary); font-size: var(--text-caption); }
	.updating { color: var(--text-secondary); font-size: var(--text-caption); display: inline-flex; align-items: center; gap: var(--space-1); }
	.time { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
	.time::before { content: '\00b7\00a0'; }
	.notes { padding: 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.5; }
	.service-actions { margin-left: auto; display: flex; align-items: center; gap: var(--space-2); }
	.automatic-row { display: flex; align-items: center; gap: var(--space-3); min-height: var(--list-row-min-height); padding: var(--space-1) var(--space-2); }
	.automatic-copy { display: grid; gap: var(--space-1); color: var(--text-primary); font-size: var(--text-subhead); }
	.time-field { border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); color: var(--text-primary); font: inherit; padding: var(--space-1); }
	@media (max-width: 768px) { .service-row, .automatic-row { flex-wrap: wrap; gap: var(--space-2); } .automatic-row .service-actions { width: auto; margin-left: auto; } .automatic-copy { flex: 1; min-width: min(250px, 100%); } }
</style>
