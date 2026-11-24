<script lang="ts">
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import ArtifactLeaf from '$lib/components/ArtifactLeaf.svelte';
	import ReviewBarShell from '$lib/components/ReviewBarShell.svelte';
	import ActivityKindPanel from './ActivityKindPanel.svelte';
	import { isPickAny, OTHER_OPTION_ID, type DecisionDetail } from '$lib/api/attentionQueue';
	import type { ActivityQueueItem } from '$lib/api/activityQueue';
	import { mutateUserTask } from '$lib/api/plans';
	import { markCommentThreadViewed } from '$lib/api/docs';
	import { draftFor } from '$lib/stores/attentionDock.svelte';
	import { submitAnswer } from '$lib/stores/attentionAnswer.svelte';
	import { decideHeld } from '$lib/stores/attentionHeld.svelte';
	import { answers } from '$lib/stores/attentionAnswerState';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import { openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
	import {
		chatReplyOf, decisionOf, docReplyOf, documentOf, findDocument, heldOf, taskOf,
		type DocumentLookup,
	} from './activityItems';

	/**
	 * The open item, in the pane the chat uses for documents. A document opens in Activity's own workspace (never a
	 * chat's), read for the chat it came from; every other kind gets the same frame. Each kind's control sits in the
	 * docked band at the bottom and settles the item through the call its own surface already uses; the list then
	 * re-reads, and the item leaves only when the server stops returning it.
	 */
	let { item, workspaceKey, onActed, onOpenChat }: {
		item: ActivityQueueItem;
		workspaceKey: string;
		/** Re-read the queue after an act landed. */
		onActed: () => Promise<void> | void;
		onOpenChat: (threadId: string) => void;
	} = $props();

	const decision = $derived(decisionOf(item));
	const held = $derived(heldOf(item));
	const task = $derived(taskOf(item));
	const docReply = $derived(docReplyOf(item));
	const chatReply = $derived(chatReplyOf(item));
	const doc = $derived(documentOf(item));
	const sourceThread = $derived(item.source.thread_id);
	const who = $derived({ name: item.source.thread_label, id: item.source.thread_id ?? item.key });

	let sending = $state(false);
	let failure = $state('');

	// Decision draft: nothing is chosen until the person chooses, even where an agent recommends an option.
	let selected = $state<string | null>(null);
	let picked = $state<string[]>([]);
	let other = $state('');

	let lookup = $state<DocumentLookup | { status: 'resolving' } | null>(null);

	// A new item starts clean: no press in flight, no failure, nothing chosen.
	let shownKey = '';
	$effect.pre(() => {
		if (item.key === shownKey) return;
		shownKey = item.key;
		sending = false;
		failure = '';
		selected = null;
		picked = [];
		other = '';
	});

	// A document item opens in Activity's workspace as its only tab; the item it replaces is closed through the
	// ordinary close (which saves a pending edit and sends the open comment batch).
	// Primitive inputs only: a re-read hands over new item objects, and the document must not reopen for that.
	const docArtifactId = $derived(doc?.artifactId ?? null);
	const itemKey = $derived(item.key);
	const landsOnReplies = $derived(item.kind === 'reply');
	$effect(() => {
		const artifactId = docArtifactId;
		const thread = sourceThread;
		const key = itemKey;
		const landOnReplies = landsOnReplies;
		if (!artifactId) {
			lookup = null;
			void closeAllBut(null);
			return;
		}
		if (!thread) {
			lookup = { status: 'missing' };
			return;
		}
		lookup = { status: 'resolving' };
		let current = true;
		void findDocument(thread, artifactId).then((found) => {
			if (!current || key !== itemKey) return;
			lookup = found;
			if (found.status !== 'found') return;
			openArtifactTab(workspaceKey, {
				artifactId,
				path: found.path,
				title: found.title,
				...(landOnReplies ? { landOn: 'new-replies' as const } : {}),
			});
			const open = get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null;
			void closeAllBut(open);
		});
		return () => {
			current = false;
		};
	});

	async function closeAllBut(keep: string | null) {
		const tabs = get(threadWorkspaces)[workspaceKey]?.tabs ?? [];
		for (const tab of tabs) {
			if (tab.instanceId !== keep) await requestArtifactTabClose(workspaceKey, tab, () => {});
		}
	}

	/** Runs one press: busy while it runs, then either the list re-reads or the reason shows under the button. */
	async function press(run: () => Promise<string | null>) {
		if (sending) return;
		sending = true;
		failure = '';
		try {
			const problem = await run();
			if (problem) failure = problem;
			else await onActed();
		} catch {
			failure = 'That didn’t go through. Try again.';
		} finally {
			sending = false;
		}
	}

	function answerFailure(id: string): string | null {
		const state = get(answers)[id];
		return state?.state === 'failed' ? state.message : null;
	}

	const answerReady = $derived.by(() => {
		if (!decision) return false;
		if (isPickAny(decision)) return true;
		if (!selected) return false;
		return selected !== OTHER_OPTION_ID || other.trim().length > 0;
	});

	function answer(d: DecisionDetail) {
		return press(async () => {
			const draft = draftFor(d.id);
			draft.selected = isPickAny(d) ? null : selected;
			draft.picked = isPickAny(d) ? [...picked] : null;
			draft.other = other;
			await submitAnswer(d);
			const problem = answerFailure(d.id);
			// A changed decision re-reads; the list then shows its new revision.
			if (problem) await onActed();
			return problem;
		});
	}

	function togglePicked(id: string) {
		picked = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];
	}

	const decide = (verb: 'approve' | 'decline') => press(async () => {
		if (!held) return null;
		await decideHeld(held, verb);
		return answerFailure(held.id);
	});

	const markTaskDone = () => press(async () => {
		if (!task) return null;
		const outcome = await mutateUserTask(task.plan_id, 'mark_done', task.node_id, task.revision,
			`mark_done:${task.node_id}:${task.revision}`);
		if (outcome.status === 'success') return null;
		if (outcome.status === 'conflict') {
			await onActed();
			return 'This task changed. Showing the latest.';
		}
		return outcome.status === 'forbidden'
			? 'You can’t change this task. Ask the agent in chat to change it.'
			: outcome.message;
	});

	const markReplyRead = () => press(async () => {
		if (!docReply) return null;
		try {
			await markCommentThreadViewed(docReply.artifact_id, docReply.root_id, docReply.reply_id);
			return null;
		} catch {
			return 'Couldn’t mark this reply read. Try again.';
		}
	});

	/** Reply reopens the document on the reply, with its comment box open under the thread, as in a document today. */
	async function reply() {
		const ws = get(threadWorkspaces)[workspaceKey];
		const tab = ws?.tabs.find((t) => t.instanceId === ws.activeTabId);
		if (!tab || tab.kind !== 'document' || lookup?.status !== 'found' || !doc) return;
		await requestArtifactTabClose(workspaceKey, tab, () => {});
		openArtifactTab(workspaceKey, { artifactId: doc.artifactId, path: lookup.path, title: lookup.title, landOn: 'new-replies' });
	}

	const primaryLabel = (label: string) => (failure ? 'Try again' : label);
	const lookupFailed = $derived(lookup?.status === 'missing' || lookup?.status === 'failed');
</script>

{#snippet failureLine()}
	{#if failure}<p class="act-failure" role="alert">{failure}</p>{/if}
{/snippet}

{#if doc}
	{#if lookupFailed}
		<ActivityKindPanel label={item.title}>
			{#snippet body()}
				<div class="unopenable" role="status">
					<h1 class="dh">Couldn’t open this document.</h1>
					<p class="dctx">
						{lookup?.status === 'failed'
							? 'Its chat’s files didn’t load.'
							: 'It isn’t in the files of the chat it came from; it may have been moved or removed.'}
					</p>
					{#if sourceThread}
						<Button size="sm" variant="secondary" onclick={() => onOpenChat(sourceThread)}>Open {item.source.thread_label}’s chat</Button>
					{/if}
				</div>
			{/snippet}
		</ActivityKindPanel>
	{:else if lookup?.status !== 'found'}
		<div class="doc-pane" aria-busy="true"></div>
	{:else}
		<div class="doc-pane">
			<ArtifactLeaf threadId={workspaceKey} targetThreadId={sourceThread} collapsible={false} mode="expanded" />
			{#if docReply}
				<div class="doc-band">
					<ReviewBarShell view="ask" {who} full="Replied to your comment" askLabel="Reply to your comment" docked busy={sending}>
						{#snippet actions()}
							<div class="rb-actions">
								<Button size="sm" variant="ghost" disabled={sending} onclick={() => void reply()}>Reply</Button>
								<Button size="sm" variant="primary" loading={sending} onclick={() => void markReplyRead()}>{primaryLabel('Done')}</Button>
							</div>
							{@render failureLine()}
						{/snippet}
					</ReviewBarShell>
				</div>
			{/if}
		</div>
	{/if}
{:else if decision}
	<ActivityKindPanel label="Decision">
		{#snippet body()}
			<h1 class="dh">{decision.title}</h1>
			{#if decision.context}<p class="dctx">{decision.context}</p>{/if}
			{#if decision.tradeoff}<p class="dctx">{decision.tradeoff}</p>{/if}
			<fieldset class="options">
				<legend class="sr-only">{isPickAny(decision) ? 'Choose any' : 'Choose one'}</legend>
				{#each decision.options as option (option.id)}
					{@const recommended = Array.isArray(decision.recommendation) ? decision.recommendation.includes(option.id) : decision.recommendation === option.id}
					<label class="opt">
						{#if isPickAny(decision)}
							<input type="checkbox" checked={picked.includes(option.id)} onchange={() => togglePicked(option.id)} disabled={sending} />
						{:else}
							<input type="radio" name="decision-{decision.id}" value={option.id} checked={selected === option.id} onchange={() => (selected = option.id)} disabled={sending} />
						{/if}
						<span><b>{option.label}</b>{#if recommended} <em>Recommended</em>{/if}{#if option.detail}<span class="opt-detail">{option.detail}</span>{/if}</span>
					</label>
				{/each}
				{#if decision.other_enabled && !isPickAny(decision)}
					<label class="opt">
						<input type="radio" name="decision-{decision.id}" value={OTHER_OPTION_ID} checked={selected === OTHER_OPTION_ID} onchange={() => (selected = OTHER_OPTION_ID)} disabled={sending} />
						<span><b>Something else</b></span>
					</label>
				{/if}
				{#if decision.other_enabled && (selected === OTHER_OPTION_ID || isPickAny(decision))}
					<textarea class="other" rows="3" bind:value={other} disabled={sending}
						placeholder={isPickAny(decision) ? 'Add a note (optional)' : 'Say what you want instead'}
						aria-label={isPickAny(decision) ? 'Note' : 'Your answer'}></textarea>
				{/if}
			</fieldset>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your answer" askLabel="Decision" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" loading={sending} disabled={!answerReady} onclick={() => void answer(decision)}>{primaryLabel('Answer')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if task}
	<ActivityKindPanel label="Task">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			{#if task.description}<p class="dctx pre">{task.description}</p>{/if}
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="A step only you can do" askLabel="Task for you" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" loading={sending} onclick={() => void markTaskDone()}>{primaryLabel('Done')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if held}
	<ActivityKindPanel label="Message">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			<p class="dctx">To: {held.recipient}{#if held.provider} on {held.provider}{/if}</p>
			<blockquote class="pre">{held.text}</blockquote>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your yes before it’s sent" askLabel="Message waiting for your yes" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="ghost" disabled={sending} onclick={() => void decide('decline')}>Don’t send</Button>
						<Button size="sm" variant="primary" loading={sending} onclick={() => void decide('approve')}>{primaryLabel('Send')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if chatReply}
	<ActivityKindPanel label="Reply" icon="chat">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			{#if chatReply.text}<blockquote class="pre">{chatReply.text}</blockquote>{/if}
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Replied to your comment" askLabel="Reply to your comment" docked>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" onclick={() => onOpenChat(chatReply.thread_id)}>Open in chat</Button>
					</div>
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else}
	<ActivityKindPanel label={item.title}>
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			{#if sourceThread}
				<Button size="sm" variant="secondary" onclick={() => onOpenChat(sourceThread)}>Open {item.source.thread_label}’s chat</Button>
			{/if}
		{/snippet}
	</ActivityKindPanel>
{/if}

<style>
	.doc-pane { position: relative; height: 100%; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	/* The reply control docks over the document's bottom edge, where a review ask docks. */
	.doc-band { position: absolute; left: 0; right: 0; bottom: 0; z-index: 5; }
	.dh { margin: 0 0 var(--space-3); font-size: var(--text-title3); line-height: 1.35; color: var(--text-primary); }
	.dctx { margin: 0 0 var(--space-3); color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; }
	.pre { white-space: pre-wrap; }
	blockquote {
		margin: var(--space-4) 0; padding: 0 0 0 var(--space-4); border-left: 1px solid var(--border-subtle);
		font-size: var(--text-callout); line-height: 1.55; color: var(--text-primary);
	}
	.options { margin: var(--space-3) 0 0; padding: 0; border: 0; }
	.opt { display: flex; gap: var(--space-3); padding: var(--space-3) 0; font-size: var(--text-subhead); cursor: pointer; }
	.opt input { flex: none; margin: var(--space-px-2) 0 0; accent-color: var(--accent); }
	.opt b { font-weight: var(--fw-semibold); color: var(--text-primary); }
	.opt em { margin-left: var(--space-2); font-style: normal; color: var(--accent-text); font-size: var(--text-footnote); }
	.opt-detail { display: block; margin-top: var(--space-1); color: var(--text-secondary); line-height: 1.45; }
	.other {
		width: 100%; box-sizing: border-box; margin-top: var(--space-1); padding: var(--space-2);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary);
		color: var(--text-primary); font: inherit; font-size: var(--text-subhead); resize: vertical;
	}
	.unopenable { padding-top: var(--space-4); }
	.act-failure {
		flex: 1 0 100%; order: 10; margin: 0; text-align: right;
		color: var(--red); font-size: var(--text-footnote);
	}
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
