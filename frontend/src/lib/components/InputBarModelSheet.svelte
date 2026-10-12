<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { activeThreadId, threads } from '$lib/stores/threads';
	import { loadCheckpoints } from '$lib/stores/checkpoints';
	import { activeModelId, beginThreadModelSwitch, finishThreadModelSwitch, getModelDispatchId, modelMatchesId, models, modelAvailability, saveThreadModel, setActiveModel, type Model } from '$lib/stores/models';
	import { accountSegments, canonicalName, groupModels, groupStatusSegments, rowNote, usageBreakdown, type ModelGroup } from '$lib/stores/modelGroups';
	import { loadPickerPolicy, pickerAccounts, policyFor, vendorRows, type PickerAccount } from '$lib/stores/accountPicker';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import AccountPickerRows from './AccountPickerRows.svelte';
	import CurrentCheck from './CurrentCheck.svelte';
	import PickerAccountHeader from './PickerAccountHeader.svelte';
	import PickerModelGroupHeader from './PickerModelGroupHeader.svelte';
	import { choosePickerAccount } from '$lib/stores/pickerAccountChoice';
	import { workspaceConductorId } from '$lib/utils/conductor';
	import { requestProviderCard } from '$lib/stores/settingsNavigation';
	import { projectSettingsOpen } from '$lib/stores/projectSettings';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { workspaceSettingsOpen } from '$lib/stores/workspaceSettings';
	import PickerLocalStatus from './PickerLocalStatus.svelte';
	import { localStatus, retryLocalSetup, watchLocalStatus } from '$lib/stores/localPickerStatus';
	import { toasts } from '$lib/stores/toast';
	import { emitProductEvent } from '$lib/product_events';

	const groups = $derived(groupModels($models, $activeModelId));
	// Ready groups start open, unusable ones stay collapsed until tapped; account-backed model rows
	// stay visible, while their account list opens only on demand, the same for every vendor and state.
	let opened = $state<Record<string, boolean>>({});
	let accountOpened = $state<Record<string, boolean>>({});
	let accountChoiceBusy = false;
	let usageShown = $state<Record<string, boolean>>({});
	let { onclose = () => {} }: { onclose?: () => void } = $props();
	const isOpen = (group: ModelGroup, hasAccounts: boolean) => opened[group.key] ?? (group.state === 'ready' || hasAccounts);
	// A collapsed group nobody can pick from still shows the thread's current model, disabled.
	const rowsShown = (group: ModelGroup, open: boolean) =>
		group.state === 'ready'
			? open ? group.rows : []
			: open ? group.blocked : group.blocked.filter((model) => modelMatchesId(model, $activeModelId));
	const activeThread = $derived(
		$threads.find((thread) => thread.id === $activeThreadId),
	);
	const pendingModelId = $derived(activeThread?.pending_model ?? null);
	const pendingStatus = $derived(activeThread?.switch_status ?? null);

	// What each account has left is the account's own state, read once when the picker opens
	// over a catalog with account-run rows; a catalog of local or external models has none.
	let accountsRequested = false;
	$effect(() => {
		if (accountsRequested || !$models.some((model) => model.runs_on?.length)) return;
		accountsRequested = true;
		void loadVendorAccounts();
		void loadPickerPolicy(activeThread?.project_id);
	});

	// A starter download, or a failed setup, shows in the Local group while the picker is open.
	const isLocalGroup = (group: ModelGroup) => group.rows.some((model) => getModelDispatchId(model).startsWith('omlx@'));
	const hasLocalGroup = $derived(groups.some(isLocalGroup));
	$effect(() => watchLocalStatus());
	async function retryLocal(kind: 'starter' | 'setup') {
		if (!(await retryLocalSetup(kind))) toasts.show("Couldn't set up the local model.", 'error');
	}

	async function chooseAccount(row: PickerAccount) {
		if (accountChoiceBusy) return;
		accountChoiceBusy = true;
		const result = await choosePickerAccount($activeThreadId, $activeModelId, $workspaceConductorId, row);
		accountChoiceBusy = false;
		if (!result.ok) toasts.show(result.message, 'error');
	}

	function openRolloverSettings() {
		threadSettingsOpen.set(false);
		if (activeThread?.project_id) {
			workspaceSettingsOpen.set(false);
			projectSettingsOpen.set(true);
		} else {
			projectSettingsOpen.set(false);
			workspaceSettingsOpen.set(true);
		}
		onclose();
	}

	// Picking a model is one action. The server folds on the model being left
	// and carries the switch through without another user decision.
	async function chooseModel(model: Model) {
		const threadId = $activeThreadId;
		const previousId = $activeModelId;
		const dispatchId = getModelDispatchId(model);
		if (modelMatchesId(model, previousId)) return;
		if (threadId && !beginThreadModelSwitch(threadId)) return;
		try {
			if (!threadId) {
				setActiveModel(dispatchId);
				emitSwitchEvent(model, dispatchId, threadId, previousId);
				return;
			}
			const t = $token;
			if (!t) throw new Error('Not authenticated');
			const result = await saveThreadModel(threadId, dispatchId, t);
			if (result.kind === 'pending') {
				if (result.checkpointRequired) {
					void loadCheckpoints(threadId, t).catch(() => {
						toasts.show('Checkpoint status will refresh on the next update.', 'info');
					});
				}
				return;
			}
			if (result.warning) toasts.show(result.warning, 'info', 6000);
			if ($activeThreadId !== threadId) return;
			if (!result.accepted || !modelMatchesId(model, result.model)) return;
			emitSwitchEvent(model, dispatchId, threadId, previousId);
		} catch (error) {
			toasts.show(
				error instanceof Error ? error.message : "Couldn't switch model",
				'error',
			);
		} finally {
			if (threadId) finishThreadModelSwitch(threadId);
		}
	}

	function emitSwitchEvent(
		model: Model,
		dispatchId: string,
		threadId: string | null,
		previousId: string | null,
	) {
		emitProductEvent('model', 'switched', {
			thread_id: threadId,
			model_id: dispatchId,
			catalog_model_id: model.id,
			provider: model.provider ?? model.backend,
			previous_model_id: previousId,
			reasoning_shape: model.capabilities?.reasoning_config?.type ?? null,
		});
	}
</script>


<div class="action-sheet model-sheet">
	<div class="sheet-section">
		<div class="sheet-section-label">Model</div>
		<div class="model-list">
			{#if groups.length === 0 && !$localStatus}
				<div class="model-list-empty">No available models</div>
			{:else}
				{#each groups as group (group.key)}
					{@const vendor = vendorRows(group.key, $models, $vendorAccounts)}
					{@const policy = $policyFor(activeThread?.project_id, group.key)}
					{@const accountRows = pickerAccounts(group.key, $models, $vendorAccounts, policy, $activeModelId)}
					{@const accountBacked = group.rows.some((model) => model.runs_on !== undefined)}
					{@const open = isOpen(group, accountRows.length > 0)}
					{@const usable = group.state === 'ready'}
					{@const shown = accountBacked ? group.rows : rowsShown(group, open)}

					{@const markedRow = accountRows.find((row) => row.marked) ?? null}
					{@const markedAccount = markedRow?.account ?? null}
					{@const selectedAccount = markedAccount ?? (vendor.length === 1 ? vendor[0] : null)}
					{@const allAccountsLimited = group.state === 'limited' && accountRows.length > 0 && !accountRows.some((row) => row.choosable)}
					{@const headerSegments = allAccountsLimited || !selectedAccount ? groupStatusSegments(group, vendor) : accountSegments(selectedAccount)}
					{@const recovery = selectedAccount?.usage?.state === 'limited' ? accountRows.find((row) => row.choosable) ?? null : null}
					{@const accountOpen = accountOpened[group.key] ?? false}
					{@const statusSegments = open && accountRows.length > 0 && (usable || group.state === 'limited') ? [] : groupStatusSegments(group, vendor)}

					{@const subtitle = usable ? statusSegments : []}
					{@const headStatus = usable ? [] : statusSegments}
					{@const breakdown = usable && vendor.length === 1 ? usageBreakdown(vendor[0]) : []}
					{@const usageOpen = usageShown[group.key] ?? false}
					<section class="model-group" class:unusable={!usable} class:signed-out={group.state === 'signed_out'} aria-label={group.name} data-state={group.state}>
							{#if accountBacked}
								<PickerAccountHeader
									vendor={group.name}
									account={allAccountsLimited ? null : selectedAccount}
									segments={headerSegments}
									chooser={accountRows.length > 0}
									open={accountOpen}
									rollover={policy?.rollover === true}
									recovery={recovery?.account}
									onchoose={$activeThreadId && recovery ? () => chooseAccount(recovery) : undefined}
									breakdown={breakdown}
									usageId={`picker-account-usage-${group.key}`}
									usageOpen={usageOpen}
									ontoggleusage={() => (usageShown = { ...usageShown, [group.key]: !usageOpen })}
									actionLabel={group.state === 'signed_out' ? 'Sign in' : undefined}
									onaction={() => requestProviderCard(group.key)}
									ontoggle={() => (accountOpened = { ...accountOpened, [group.key]: !accountOpen })}
								/>
								{#if accountOpen && accountRows.length > 0}
									<AccountPickerRows rows={accountRows} />
									{#if policy?.rollover === true}
										<div class="rollover-settings"><button type="button" onclick={openRolloverSettings}>Rollover settings</button></div>
									{/if}
								{/if}
							{:else}
						<PickerModelGroupHeader
							name={group.name}
							groupKey={group.key}
							count={group.rows.length}
							local={isLocalGroup(group) && usable}
							open={open}
							status={headStatus}
							subtitle={subtitle}
							breakdown={breakdown}
							usageOpen={usageOpen}
							onToggle={() => (opened = { ...opened, [group.key]: !open })}
							onToggleUsage={() => (usageShown = { ...usageShown, [group.key]: !usageOpen })}
						/>
						{#if open && accountRows.length > 0}<AccountPickerRows rows={accountRows} />{/if}


							{/if}
						{#if $localStatus && isLocalGroup(group)}<PickerLocalStatus line={$localStatus.line} retry={$localStatus.retry} onretry={retryLocal} />{/if}
						{#if shown.length > 0}
							<div class="model-group-rows" id={`model-group-${group.key}`}>
								{#each shown as model (model.id)}
												{@const caps = model.capabilities}
												{@const note = usable ? rowNote(model, markedAccount) : null}
												{@const blocked = modelAvailability(model).status !== 'ready' || !usable}
									<button
										class="model-list-item press-tint"
										class:active={modelMatchesId(model, $activeModelId)}
										class:pending={modelMatchesId(model, pendingModelId)}
										class:blocked
										disabled={blocked}
										onclick={() => chooseModel(model)}
									>
										<span class="model-status-dot" style={`background: var(${blocked ? '--red' : '--green'})`}></span>
										<span class="model-list-name">{canonicalName(model)}</span>
										{#if note}
											<span class="row-note" class:usage-amber={note.level === 'amber'} class:usage-red={note.level === 'red'} aria-label={note.label}>{note.text}</span>
										{/if}
										{#if modelMatchesId(model, pendingModelId)}
											<span
												class="switch-marker"
												class:failed={pendingStatus === 'failed'}
												title={activeThread?.switch_error ?? undefined}
											>
												{pendingStatus === 'failed' ? 'Switch failed' : 'Switching…'}
											</span>
										{/if}
										{#if model.cache_marker === 'Cached'}
											<span class="cached-marker">Cached</span>
										{/if}
										<span class="model-caps">
											{#if caps?.reasoning_config}
												<svg class="cap-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Reasoning"><path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-3 3 3 3 0 0 0 1 5 3 3 0 0 0 5 1 3 3 0 0 0 3-3V4Z"/><path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 3 3 3 3 0 0 1-1 5 3 3 0 0 1-5 1 3 3 0 0 1-3-3V4Z"/></svg>
											{/if}
											{#if caps?.supports_vision}
												<svg class="cap-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Vision"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
											{/if}
											{#if caps?.supports_tools}
												<svg class="cap-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Tools"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
											{/if}
										</span>
										{#if modelMatchesId(model, $activeModelId)}
											<CurrentCheck />
										{/if}
									</button>
								{/each}
							</div>
						{/if}
					</section>
				{/each}
				{#if $localStatus && !hasLocalGroup}
					<section class="model-group" aria-label="Local">
						<PickerModelGroupHeader name="Local" groupKey="local" count={0} fallback />
						<PickerLocalStatus line={$localStatus.line} retry={$localStatus.retry} onretry={retryLocal} />
					</section>
				{/if}
			{/if}
		</div>
	</div>
</div>

<style>
	.cached-marker {
		font-size: var(--text-caption);
		color: var(--text-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: 999px;
		padding: 1px 6px;
	}

	.switch-marker {
		font-size: var(--text-caption);
		color: var(--accent);
	}

	.switch-marker.failed {
		color: var(--red);
	}

	/* The sheet's own width belongs to the shared .action-sheet (InputBar.css): a scoped
	   width rule here outranks it and lets the picker span the whole column on desktop. */
	.model-list,
	.model-group,
	.model-group-rows {
		min-width: 0;
		max-width: 100%;
	}

	.model-group + .model-group {
		margin-top: 4px;
	}


	.model-group.signed-out .model-group-rows {
		opacity: .52;
	}

	.model-group.signed-out .model-status-dot {
		background: var(--text-tertiary) !important;
	}

	.row-note {
		font-size: var(--text-caption);
		color: var(--text-tertiary);
	}

	.row-note.usage-amber {
		color: var(--amber-text);
	}

	.row-note.usage-red {
		color: var(--red-text);
	}

	.model-group-rows {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding-left: 22px;
	}

	.model-list-item .model-list-name {
		white-space: normal;
		overflow-wrap: anywhere;
	}

	.model-list-item.blocked {
		cursor: default;
	}

	.rollover-settings { padding: 2px 10px 6px 32px; }
	.rollover-settings button { padding: 0; color: var(--accent); font: inherit; font-size: var(--text-footnote); background: none; border: 0; cursor: pointer; }

	.row-note {
		min-width: 0;
		max-width: 45%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
