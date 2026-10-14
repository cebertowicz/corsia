<script lang="ts">
	import { onDestroy } from 'svelte';
	import { isMobile } from '$lib/stores/viewport';
	import { drainStatus } from '$lib/stores/connection';
	import { toasts } from '$lib/stores/toast';
	import { Button, Modal, Toggle } from '$lib/components/ui';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import { failureCopy, failureFromOperation, failureFromVerdict } from './failureCopy';
	import { UPDATE_STEPS, currentOperation, elapsedSeconds, formatElapsed, formatInstalledAt, formatScheduleTime, humanDuration, isFailedOperation, isInFlight, releaseNotesFor, stepDetail, stepIndex, stepMessage } from './model';
	import { markOutcomeSeen } from './outcome';
	import { restartToFinishUpdate } from './restartToFinish';
	import { closeUpdateSurface, openUpdatePanel, retryUpdate, saveUpdateSchedule, startUpdate, updateFailureVerdict, updateInFlight, updateOperation, updateServerClockOffsetMs, updateStarting, updateStatus, updateSurface, updateUnreachable } from './store';

	let now = $state(Date.now());
	let showAll = $state(false);
	const tick = setInterval(() => { now = Date.now(); }, 1_000);
	onDestroy(() => clearInterval(tick));

	const operation = $derived($updateOperation);
	const current = $derived(currentOperation(operation, $updateStatus));
	const failed = $derived(isFailedOperation(current));
	const completed = $derived(current?.status === 'complete');
	const running = $derived(isInFlight(operation));
	const panel = $derived($updateSurface === 'panel' || running);
	// A verdict the client reached itself wins over whatever the latest operation says.
	const failureDetail = $derived($updateFailureVerdict ? failureFromVerdict($updateFailureVerdict) : current ? failureFromOperation(current) : null);
	const failure = $derived(failureDetail ? failureCopy(failureDetail) : null);
	const elapsed = $derived(formatElapsed(elapsedSeconds(operation, now, $updateServerClockOffsetMs)));
	const liveScreen = $derived(failed || $updateFailureVerdict ? 'failure' : panel ? 'panel' : completed ? 'notes' : 'sheet');
	// The surface keeps its last screen while it plays its closing motion.
	let shownScreen: typeof liveScreen = 'sheet';
	const screen = $derived.by(() => {
		if ($updateSurface !== 'closed') shownScreen = liveScreen;
		return shownScreen;
	});
	const surfaceOpen = $derived($updateSurface !== 'closed');
	const installedAt = $derived(completed && current?.settled_at ? formatInstalledAt(current.settled_at, now) : null);
	const notes = $derived(releaseNotesFor(current, $updateStatus));
	const targetVersion = $derived(notes?.targetVersion ?? operation?.target_version ?? $updateStatus?.target_version ?? 'the latest version');
	const heading = $derived(screen === 'failure' ? (failure?.title ?? "Update didn't start") : screen === 'panel' ? 'Updating CORSIA' : screen === 'notes' ? `What's new in ${targetVersion}` : 'Update CORSIA');
	const agentCount = $derived($drainStatus?.activeTurns ?? $updateStatus?.agents_working ?? 0);
	const activeStep = $derived(stepIndex(operation, $updateUnreachable));
	const schedule = $derived($updateStatus?.schedule ?? { automatic: false, time: '03:00', tonight_at: null });

	// Closing the progress toast hides it for the rest of that operation; the Updated and failure toasts are pinned elsewhere and always show.
	let hiddenProgressFor: string | null = $state(null);

	$effect(() => {
		if (!running) return;
		if ($updateSurface === 'panel') { toasts.unpin('update'); return; }
		const operationId = operation?.operation_id ?? 'starting';
		if (hiddenProgressFor === operationId) { toasts.unpin('update'); return; }
		toasts.pin('update', { onClose: () => { hiddenProgressFor = operationId; }, type: 'progress', message: stepMessage(operation, $updateUnreachable), time: elapsed, closeLabel: 'Hide progress', action: { label: 'Details', run: openUpdatePanel } });
	});

	// Closing a failure is acting on it; until then it stays visible on every device.
	function close(): void {
		if (screen === 'failure' && failed && current) void markOutcomeSeen(current.operation_id);
		closeUpdateSurface();
	}
	function retry(): void { void (failure?.retryAction === 'restart' ? restartToFinishUpdate() : retryUpdate()); }
	function updateNow(): void { void startUpdate(); }
	async function tonight(): Promise<void> {
		if (!await saveUpdateSchedule({ tonight: true })) return;
		closeUpdateSurface();
		toasts.showAction(`Update scheduled for ${formatScheduleTime(schedule.time)} tonight.`, {
			label: 'Undo', run: () => { void saveUpdateSchedule({ tonight: false }); },
		});
	}
	function visibleChanges() { return showAll ? (notes?.changes?.items ?? []) : (notes?.changes?.items ?? []).slice(0, 3); }
	function totalChanges() { return (notes?.changes?.items.length ?? 0) + (notes?.changes?.more ?? 0); }
	function detailLine(value: string | null | undefined): string | null {
		return value ? `${value.replace(/\.$/, '')}.` : null;
	}
</script>

<!-- Each surface toggles at its own block so its closing motion plays: a parent block removing it would skip the outro. -->
{#if $isMobile}
	<AdaptiveDrawer ariaLabel={heading} {heading} doneLabel={screen === 'sheet' ? 'Later' : screen === 'panel' ? 'Hide' : screen === 'notes' ? 'Done' : 'Close'} open={surfaceOpen} onClose={close} fill>
		{#if screen === 'sheet' || screen === 'notes'}{@render decision()}{:else if screen === 'panel'}{@render progress()}{:else}{@render failurePanel()}{/if}
	</AdaptiveDrawer>
{:else if surfaceOpen}
	<Modal ariaLabel={heading} onClose={close} head={modalHead} foot={modalFoot}>
		{#if screen === 'sheet' || screen === 'notes'}{@render decision()}{:else if screen === 'panel'}{@render progress()}{:else}{@render failurePanel()}{/if}
	</Modal>
{/if}

{#snippet modalHead()}
	<header class:failure-head={screen === 'failure'} class="update-head">
		{#if screen === 'failure'}<svg class="warning" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v4m0 3h.01"/></svg>{/if}
		<h2>{heading}</h2>
		{#if screen === 'sheet' || screen === 'notes'}<button type="button" class="close" aria-label="Close" onclick={close}>×</button>{/if}
	</header>
{/snippet}

{#snippet modalFoot()}
	<div class="update-foot">
		{#if screen === 'sheet'}<Button variant="ghost" onclick={close}>Later</Button>{#if !schedule.automatic}<Button variant="secondary" onclick={() => void tonight()}>Tonight at {formatScheduleTime(schedule.time)}</Button>{/if}<Button variant="primary" loading={$updateStarting} onclick={updateNow}>Update now</Button>
		{:else if screen === 'panel'}<Button variant="ghost" onclick={close}>Hide</Button>
		{:else if screen === 'notes'}<Button variant="primary" onclick={close}>Done</Button>
		{:else}{#if failure?.retryLabel}<Button loading={$updateStarting} onclick={retry}>{failure.retryLabel}</Button>{/if}<Button variant="primary" onclick={close}>Close</Button>{/if}
	</div>
{/snippet}

{#snippet decision()}
	<section class="update-content decision">
		{#if screen !== 'notes'}
			<p class="version">Version {targetVersion}{#if $updateStatus?.current_version}{` · you're on ${$updateStatus.current_version}`}{/if}</p>
		{:else if installedAt}
			<p class="version">Installed {installedAt}</p>
		{/if}
		{#if notes?.changes?.headline}<p class="headline">{notes.changes.headline}</p>{/if}
		{#if notes?.changes}
			{#if screen !== 'notes'}<h3>What's new</h3>{/if}
			<ul>{#each visibleChanges() as item}<li>{item}</li>{/each}</ul>
			{#if totalChanges() > 3}<button class="show-all" onclick={() => showAll = !showAll}>{showAll ? 'Show fewer changes' : `Show all ${totalChanges()} changes`}<svg class="chevron" class:expanded={showAll} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>{/if}
		{/if}
		{#if screen === 'sheet' && $updateStatus?.drain_cap_seconds}
			<div class="cost" data-update-drain><strong>CORSIA will be unavailable — usually about a minute.</strong>{#if agentCount > 0}<span>{#if agentCount === 1}1 agent is working: it gets up to {humanDuration($updateStatus.drain_cap_seconds)} to reach a safe point. If it is still mid-step, it is stopped and starts that step again once CORSIA is back.{:else}{agentCount} agents are working: they get up to {humanDuration($updateStatus.drain_cap_seconds)} to reach a safe point. Any still mid-step are stopped and start that step again once CORSIA is back.{/if}</span>{/if}</div>
		{/if}
		{#if screen === 'sheet'}<div class="automatic-choice"><div><span>Keep CORSIA up to date automatically</span>{#if schedule.automatic}<span>Installs at {formatScheduleTime(schedule.time)}. You'll see what changed the next time you open CORSIA.</span>{/if}</div><Toggle checked={schedule.automatic} onchange={(next) => void saveUpdateSchedule({ automatic: next })} ariaLabel="Keep CORSIA up to date automatically" /></div>{/if}
		{#if screen === 'sheet' && $isMobile}<div class="mobile-actions" data-update-mobile-actions>{#if !schedule.automatic}<Button variant="secondary" onclick={() => void tonight()}>Tonight at {formatScheduleTime(schedule.time)}</Button>{/if}<Button variant="primary" loading={$updateStarting} onclick={updateNow}>Update now</Button></div>{/if}
	</section>
{/snippet}

{#snippet progress()}
	<section class="update-content progress" aria-live="polite">
		<p class="version">Updating to {targetVersion}</p>
		<ol class="steps">{#each UPDATE_STEPS as step, index}<li class:active={activeStep === index} class:done={activeStep > index} aria-current={activeStep === index ? 'step' : undefined}><span class="step-mark" aria-hidden="true">{#if activeStep > index}✓{:else if activeStep === index}<span class="spinner"></span>{/if}</span>{#if activeStep > index}<span aria-hidden="true">{step}</span><span class="sr-only">{step}, done</span>{:else}{step}{/if}</li>{/each}</ol>
		{#if stepDetail(operation, $updateUnreachable)}<p class="step-detail">{stepDetail(operation, $updateUnreachable)}</p>{/if}
		<div class="timing"><span>Elapsed <strong>{elapsed}</strong></span>{#if $updateStatus?.usual_seconds != null}<span>Usually about {humanDuration($updateStatus.usual_seconds)}</span>{/if}</div>
		{#if activeStep === 2}<p class="muted">This page comes back on its own. Reloading now shows a connection error until it's back.</p>{/if}
	</section>
{/snippet}

{#snippet failurePanel()}
	<section class="update-content failure">
		<p class="headline">{failure?.headline ?? "The update didn't start."}</p>
		{#if failure?.body}<p>{failure.body}</p>{/if}
		{#if failureDetail?.conductorTold}<p class="conductor"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 5a3 3 0 0 1 0 6m2 3c1.8.5 3 2.1 3 4"/></svg>Your workspace conductor has been told.</p>{/if}
		{#if detailLine(failureDetail?.summary) || failureDetail?.detail || failureDetail?.log}<details><summary>Details <svg class="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></summary>{#if detailLine(failureDetail?.summary)}<pre>{detailLine(failureDetail?.summary)}</pre>{/if}{#if failureDetail?.detail}<pre>{failureDetail.detail}</pre>{/if}{#if failureDetail?.log}<pre>{failureDetail.log}</pre>{/if}</details>{/if}
		{#if $isMobile && failure?.retryLabel}<div class="mobile-actions"><Button loading={$updateStarting} onclick={retry}>{failure.retryLabel}</Button></div>{/if}
	</section>
{/snippet}

<style>
	.update-head { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-4) var(--space-5); border-bottom: 1px solid var(--border-subtle); }
	.update-head h2 { flex: 1; margin: 0; color: var(--text-primary); font-size: var(--text-body); }
	.failure-head { border-bottom: 0; } .warning { width: 18px; stroke: var(--amber, var(--red)); fill: none; stroke-width: 2; }
	.close, .show-all { border: 0; background: transparent; color: var(--accent); font: inherit; cursor: pointer; } .close { font-size: var(--text-title3); } .show-all, summary { display: inline-flex; align-items: center; gap: var(--space-1); } .chevron { transition: transform var(--dur-tight); } .chevron.expanded, details[open] .chevron { transform: rotate(180deg); }
	.update-foot { display: flex; justify-content: flex-end; gap: var(--space-2); width: 100%; }
	.update-content { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; }
	.version { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); } .headline { color: var(--text-primary); font-size: var(--text-callout); font-weight: var(--fw-semibold); }
	h3 { margin: var(--space-5) 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); letter-spacing: .06em; text-transform: uppercase; }
	ul { margin: 0; padding-left: var(--space-5); } .version + ul { margin-top: var(--space-3); } li + li { margin-top: var(--space-1); } .show-all { margin-top: var(--space-2); padding: 0; }
	.cost { display: grid; gap: var(--space-2); margin-top: var(--space-5); padding: var(--space-3); border-radius: var(--radius-sm); background: var(--violet-bg); } .cost strong { color: var(--text-primary); }
	.automatic-choice { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); margin-top: var(--space-5); padding-top: var(--space-4); border-top: 1px solid var(--border-subtle); color: var(--text-primary); } .automatic-choice div { display: grid; gap: var(--space-1); } .automatic-choice div span:last-child { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.muted { color: var(--text-tertiary); } .steps { display: grid; gap: var(--space-3); margin: var(--space-3) 0; padding: 0; list-style: none; }
	.steps li { display: flex; align-items: center; gap: var(--space-2); color: var(--text-tertiary); } .steps li.active { color: var(--text-primary); font-weight: var(--fw-semibold); } .steps li.done { color: var(--green-text); }
	.step-mark { width: 18px; height: 18px; display: inline-grid; place-items: center; border: 1px solid var(--border); border-radius: var(--radius-circle); } .done .step-mark { border-color: transparent; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	.spinner { width: 10px; height: 10px; border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent); border-top-color: var(--accent); border-radius: var(--radius-circle); animation: spin .8s linear infinite; }
	.step-detail { margin: var(--space-4) 0; } .timing { display: flex; justify-content: space-between; gap: var(--space-3); color: var(--text-tertiary); font-size: var(--text-footnote); } .timing strong { color: var(--text-primary); }
	.conductor { display: flex; gap: var(--space-2); align-items: center; } details { border-top: 1px solid var(--border-subtle); margin-top: var(--space-4); padding-top: var(--space-3); } summary { cursor: pointer; color: var(--text-primary); } pre { max-height: 180px; overflow: auto; white-space: pre-wrap; color: var(--text-tertiary); font: var(--text-caption)/1.45 var(--font-mono, monospace); }
	.mobile-actions { display: flex; justify-content: flex-end; gap: var(--space-2); margin-top: var(--space-5); } @keyframes spin { to { transform: rotate(360deg); } } @media (max-width: 768px) { .update-content { padding: 0 0 var(--space-5); } .update-content.decision { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; } .decision .mobile-actions { position: sticky; bottom: 0; margin-bottom: calc(-1 * var(--space-5)); padding: var(--space-3) 0 var(--space-5); border-top: 1px solid var(--border-subtle); background: var(--bg-secondary); } }
</style>
