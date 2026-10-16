<!--
  AttentionCardActions.svelte — S2 clearing + S4 disposition controls.
  Extracted from AttentionCard to stay under the 400-line budget.
  Owns all action state (busy, reason input, defer modal, overflow).
  errorMsg is $bindable so the parent card can surface it above the footer.
-->
<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { Button, IconButton, Modal, Field } from '$lib/components/ui';
	import { clearAttention, disposeAttention } from '$lib/api/attention';
	import type { AttentionItem } from '$lib/api/attention';

	interface Props {
		item: AttentionItem;
		mode: 'user' | 'parent' | 'readonly';
		isDeferred: boolean;
		isCleared: boolean;
		hasBlockingReview: boolean;
		onDone?: (artifactId: string, outcome: string) => void;
		errorMsg: string | null;
	}

	let {
		item, mode, isDeferred, isCleared, hasBlockingReview, onDone,
		errorMsg = $bindable(null),
	}: Props = $props();

	let busy = $state(false);
	let showReasonInput = $state(false);
	let reasonAction = $state('');
	let reasonValue = $state('');
	let showDefer = $state(false);
	let deferDate = $state('');
	let overflowOpen = $state(false);

	const artifactId = $derived(item.external_id);

	// ── S2 clearing ───────────────────────────────────────────────────────────
	async function clear(outcome: string, opts?: { condition?: Record<string, unknown> }) {
		busy = true;
		errorMsg = null;
		try {
			const tok = get(token);
			if (!tok) throw new Error('Not authenticated');
			await clearAttention(tok, artifactId, outcome, { ...opts, hold_ids: item.hold_ids });
			onDone?.(artifactId, outcome);
		} catch (e: unknown) {
			const stale = (e as { status?: number; body?: string }).status === 409
				&& (e as { body?: string }).body?.includes('ask_stale');
			errorMsg = stale ? 'This review request has ended.' : e instanceof Error ? e.message : 'Action failed';
			if (stale) onDone?.(artifactId, outcome);
		} finally {
			busy = false;
		}
	}

	function openDefer() { deferDate = ''; showDefer = true; }

	/** Convert the datetime-local (naive, local) value to canonical UTC ISO so the
	 * backend stores/compares one timezone-aware form. */
	function deadlineUtc(local: string): string {
		return new Date(local).toISOString();
	}

	async function submitDefer() {
		if (!deferDate) return;
		showDefer = false;
		await clear('defer', { condition: { type: 'deadline', value: deadlineUtc(deferDate) } });
	}

	// ── S4 parent disposition ─────────────────────────────────────────────────
	async function dispose(
		outcome: string,
		opts?: {
			reason?: string; answer?: string; authority?: string;
			override_blocking?: boolean; new_owner?: string;
			condition?: Record<string, unknown>;
		}
	) {
		busy = true;
		errorMsg = null;
		overflowOpen = false;
		try {
			const tok = get(token);
			if (!tok) throw new Error('Not authenticated');
			await disposeAttention(tok, artifactId, outcome, { ...opts, hold_ids: item.hold_ids });
			onDone?.(artifactId, outcome);
		} catch (e: unknown) {
			const response = e as { status?: number; body?: string };
			const stale = response.status === 409 && response.body?.includes('ask_stale');
			const msg = e instanceof Error ? e.message : 'Action failed';
			errorMsg = stale ? 'This review request has ended.' : msg.includes('409') || msg.toLowerCase().includes('blocking')
				? 'A review profile flagged a blocking issue. Resolve it or supply an authorized override before approving.'
				: msg;
			if (stale) onDone?.(artifactId, outcome);
		} finally {
			busy = false;
		}
	}

	function startReason(action: string) {
		reasonAction = action; reasonValue = ''; showReasonInput = true; overflowOpen = false;
	}

	async function submitReason() {
		showReasonInput = false;
		if (reasonAction === 'reject_with_reason') {
			await dispose('reject_with_reason', { reason: reasonValue });
		} else if (reasonAction === 'request_revision') {
			await dispose('request_revision', { reason: reasonValue });
		} else if (reasonAction === 'answer_internally') {
			await dispose('answer_internally', { answer: reasonValue });
		}
		reasonValue = '';
	}

	function openDeferParent() { deferDate = ''; showDefer = true; overflowOpen = false; }

	async function submitDeferParent() {
		if (!deferDate) return;
		showDefer = false;
		await dispose('defer_until', { condition: { type: 'deadline', value: deadlineUtc(deferDate) } });
	}
</script>

<!-- Inline reason / answer input -->
{#if showReasonInput}
	<div class="reason-row">
		<Field label={reasonAction === 'answer_internally' ? 'Answer' : 'Reason'}>
			<textarea
				class="reason-input"
				rows="2"
				placeholder={reasonAction === 'answer_internally' ? 'Provide an answer…' : 'Add a reason…'}
				bind:value={reasonValue}
			></textarea>
		</Field>
		<div class="reason-actions">
			<Button size="sm" variant="ghost" onclick={() => { showReasonInput = false; }}>Cancel</Button>
			<Button size="sm" variant="primary" disabled={!reasonValue.trim() || busy} onclick={submitReason}>
				Submit
			</Button>
		</div>
	</div>
{/if}

<!-- Footer actions -->
{#if !isCleared && mode !== 'readonly'}
	<footer class="card-actions">
		{#if mode === 'user'}
			<!-- S2 clearing controls: answer / acknowledge / dismiss / defer / supersede -->
			<Button size="sm" variant="primary" disabled={busy} onclick={() => clear('answer')}>
				Answer
			</Button>
			<Button size="sm" variant="secondary" disabled={busy} onclick={() => clear('acknowledge')}>
				Acknowledge
			</Button>
			<Button size="sm" variant="ghost" disabled={busy} onclick={() => clear('dismiss')}>
				Dismiss
			</Button>
			<div class="spacer"></div>
			{#if !isDeferred}
				<IconButton ariaLabel="Defer" size="sm" variant="ghost" title="Defer until a later date" onclick={openDefer}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
				</IconButton>
			{/if}
			<div class="overflow-wrapper">
				<IconButton ariaLabel="More options" size="sm" variant="ghost" onclick={() => overflowOpen = !overflowOpen}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>
				</IconButton>
				{#if overflowOpen}
					<div class="overflow-menu" role="menu">
						<button role="menuitem" class="overflow-item danger" onclick={() => { overflowOpen = false; clear('supersede'); }}>
							Supersede
						</button>
					</div>
				{/if}
			</div>

		{:else if mode === 'parent'}
			<!-- S4 disposition controls: approve / revise / answer-internally / reject / overflow -->
			<Button
				size="sm"
				variant="primary"
				disabled={busy || (hasBlockingReview && !isDeferred)}
				title={hasBlockingReview ? 'Blocked: review profile has a blocking issue' : 'Approve for user'}
				onclick={() => dispose('approve_to_user')}
			>
				Approve
			</Button>
			<Button size="sm" variant="secondary" disabled={busy} onclick={() => startReason('request_revision')}>
				Revise
			</Button>
			<Button size="sm" variant="secondary" disabled={busy} onclick={() => startReason('answer_internally')}>
				Answer internally
			</Button>
			<Button size="sm" variant="ghost" disabled={busy} onclick={() => startReason('reject_with_reason')}>
				Reject
			</Button>
			<div class="spacer"></div>
			<div class="overflow-wrapper">
				<IconButton ariaLabel="More options" size="sm" variant="ghost" onclick={() => overflowOpen = !overflowOpen}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>
				</IconButton>
				{#if overflowOpen}
					<div class="overflow-menu" role="menu">
						{#if !isDeferred}
							<button role="menuitem" class="overflow-item" onclick={openDeferParent}>
								Defer until…
							</button>
						{/if}
						<button role="menuitem" class="overflow-item danger" onclick={() => { overflowOpen = false; dispose('supersede'); }}>
							Supersede
						</button>
					</div>
				{/if}
			</div>
		{/if}
	</footer>
{:else if isCleared}
	<footer class="card-actions cleared-footer">
		<span class="cleared-label">Cleared</span>
	</footer>
{/if}

<!-- Defer modal (shared by user-clear defer + parent defer-until) -->
{#if showDefer}
	<Modal title="Defer until" size="sm" onClose={() => showDefer = false}>
		<Field label="Deadline">
			<input type="datetime-local" bind:value={deferDate} />
		</Field>
		{#snippet foot()}
			<Button variant="ghost" onclick={() => showDefer = false}>Cancel</Button>
			<span style="flex:1"></span>
			<Button
				variant="primary"
				disabled={!deferDate}
				onclick={mode === 'parent' ? submitDeferParent : submitDefer}
			>
				Defer
			</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	.reason-row { display: flex; flex-direction: column; gap: 6px; }
	.reason-input { resize: vertical; min-height: 52px; width: 100%; }
	.reason-actions { display: flex; justify-content: flex-end; gap: 6px; }

	.card-actions {
		display: flex; align-items: center; gap: 6px;
		flex-wrap: wrap; padding-top: 4px;
	}
	.spacer { flex: 1; }
	.cleared-footer { justify-content: flex-end; }
	.cleared-label { font-size: var(--text-footnote); color: var(--text-tertiary); font-style: italic; }

	.overflow-wrapper { position: relative; }
	.overflow-menu {
		position: absolute; right: 0; bottom: calc(100% + 4px);
		background: var(--bg-elevated, var(--bg-tertiary));
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		box-shadow: var(--glass-elevation-2, 0 4px 16px rgba(0,0,0,0.18));
		min-width: 140px; z-index: 30; overflow: hidden;
	}
	.overflow-item {
		display: block; width: 100%; text-align: left;
		padding: 8px 12px; font-size: var(--text-subhead);
		color: var(--text-secondary); background: none; border: none;
		cursor: pointer; transition: background var(--dur-tight);
	}
	.overflow-item:hover { background: var(--bg-hover); color: var(--text-primary); }
	.overflow-item.danger:hover { background: var(--red-bg-hover, var(--red-bg)); color: var(--red); }

	@media (max-width: 600px) { .card-actions { gap: 8px; } }
</style>
