<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { loadPickerPolicy, policyFor } from '$lib/stores/accountPicker';
	import type { Model } from '$lib/stores/models';
	import { limitHeldLine, OVERAGE_SWITCH_LABEL, SWITCH_MODEL_LABEL } from '$lib/stores/overageSetting';
	import { requestProviderCard } from '$lib/stores/settingsNavigation';
	import { pinThreadAccount } from '$lib/stores/threadAccountPin';
	import { composerSelectionFor } from '$lib/stores/composerSelection';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';

	// The composer passes the chip's own model and picker opener, so the line
	// and the chip read one row and open one picker. The line clears on the
	// models_updated reload the backend pushes when the verdict changes, and on
	// the one the account move triggers itself.
	const { model, onSwitchModel, threadId = undefined }: {
		model: Model | undefined;
		onSwitchModel: () => void;
		threadId?: string | null;
	} = $props();

	// The line is about the thread the composer is bound to, which is a worker's own in a worker's pane.
	const sel = $derived($composerSelectionFor(threadId));
	const boundThreadId = $derived(sel.threadId);
	const projectId = $derived(sel.thread?.project_id ?? null);
	const policy = $derived(model?.connection ? $policyFor(projectId, model.connection) : undefined);

	// Moving the thread needs a thread; a draft chat has none, so it keeps the line every vendor shares.
	const line = $derived(limitHeldLine(
		model,
		new Date(),
		boundThreadId && model?.connection ? { accounts: $vendorAccounts, policy } : undefined,
	));

	let busy = $state(false);
	let error = $state('');

	async function continueOn(accountId: string) {
		const target = boundThreadId;
		if (!target || busy) return;
		busy = true;
		error = '';
		const result = await pinThreadAccount(target, accountId);
		busy = false;
		if (!result.ok) error = result.message;
	}

	// The account sentences need the accounts' labels and the rollover order, which the picker reads when
	// it opens; a held line shows before that, so read what is missing once per thread and connection when
	// several accounts could run the model. Either may already be loaded (Settings loads the accounts), so
	// each is judged on its own.
	let accountsRequestedFor = '';
	let policyRequestedFor = '';
	$effect(() => {
		const runs = model?.runs_on ?? [];
		const boundId = runs.find((entry) => entry.selected)?.account_id;
		const connection = model?.connection;
		const target = boundThreadId;
		if (!target || !connection || runs.length < 2 || !boundId || !limitHeldLine(model)) return;
		const key = `${target}:${connection}`;
		if (accountsRequestedFor !== key && !$vendorAccounts.some((account) => account.id === boundId)) {
			accountsRequestedFor = key;
			void loadVendorAccounts();
		}
		if (policyRequestedFor !== key && !policy) {
			policyRequestedFor = key;
			void loadPickerPolicy(projectId);
		}
	});

	// A refusal belongs to the thread it came from.
	$effect(() => {
		void boundThreadId;
		error = '';
	});
</script>

{#if line && model}
	<div class="limit-held" role="status">
		<span class="limit-held-text">{line.text}</span>
		{#if line.action}
			{@const action = line.action}
			<Button variant="secondary" size="sm" disabled={busy} onclick={() => continueOn(action.accountId)}>{action.label}</Button>
		{:else if !line.allLimited}
			<Button variant="secondary" size="sm" onclick={onSwitchModel}>{SWITCH_MODEL_LABEL}</Button>
		{/if}
		{#if line.planLimit && model.connection}
			{@const connection = model.connection}
			<Button variant="secondary" size="sm" onclick={() => requestProviderCard(connection)}>{OVERAGE_SWITCH_LABEL}</Button>
		{/if}
		{#if error}<span class="limit-held-error" role="alert">{error}</span>{/if}
	</div>
{/if}

<style>
	.limit-held {
		max-width: 760px; margin: 0 auto 6px;
		display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px;
		padding: 4px 4px 4px 10px;
		border-radius: var(--radius-xs);
		background: var(--bg-secondary);
		border: 1px solid var(--border-subtle);
		font-size: var(--text-footnote);
	}
	.limit-held-text {
		flex: 1 1 auto; min-width: 0;
		color: var(--red-text);
	}
	.limit-held-error {
		flex: 1 0 100%;
		color: var(--red-text);
	}
</style>
