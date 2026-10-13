<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import { localDayClock } from '$lib/time';
	import { indentVar } from './planIndent';
	import { localizeInstants, nodeDisplayLabel } from './planDisplay';
	import {
		artifactCount, dollarLine, initiativeLine, isFinished, nextMove, ownDiffers, tokenLine,
		tryEnding, waitingTime, withFollowUps, workingTime,
	} from './nodeDetailsText';

	let {
		node,
		id,
		indent = indentVar(node.depth, 0),
	}: {
		node: OperatorNode;
		id: string;
		/** The row's indent as a CSS length, so details sit directly under it (`planIndent`). */
		indent?: string;
	} = $props();

	// One fact per line (shared/planning/plans-node-details-design-2026-09-24.md,
	// revision 3). A rendered fact never changes control type or hides more text.
	const execution = $derived(node.execution);
	const reason = $derived(execution.primary_reason ?? node.status_reason);
	const label = $derived(nodeDisplayLabel(node));
	const tone = $derived(node.display.tone_key);
	const causes = $derived(execution.causes.length > 1 ? execution.causes : []);
	const showTries = $derived(execution.attempt_count > 0 || node.state === 'failed');
	const next = $derived(nextMove(execution.causes));
	const showEvidence = $derived(isFinished(node) || execution.condition === 'review');
	const staleEvidence = $derived(
		node.evidence !== null && node.evidence.requirements_revision < node.requirements_revision,
	);
	const evidenceNotes = $derived([
		...(staleEvidence ? ['Predates the latest requirements'] : []),
		...(node.evidence?.artifact_ids.length ? [artifactCount(node.evidence.artifact_ids.length)] : []),
	]);
	const rolled = $derived(node.node_cost.rolled);
	const own = $derived(node.node_cost.own);
	const working = $derived(workingTime(rolled));
	const waiting = $derived(waitingTime(rolled));
	const tokenUsage = $derived(tokenLine(rolled));
	const cost = $derived(dollarLine(rolled));
	const showOwn = $derived(ownDiffers(node));
	const ownWorking = $derived(showOwn && workingTime(own) !== working ? workingTime(own) : null);
	const ownWaiting = $derived(showOwn && waitingTime(own) !== waiting ? waitingTime(own) : null);
	const ownTokens = $derived(showOwn && tokenLine(own) !== tokenUsage ? tokenLine(own) : null);
	const ownCost = $derived(showOwn && dollarLine(own) !== cost ? dollarLine(own) : null);
	const initiative = $derived(initiativeLine(node));
	// One total on a lineage of two or more tasks: the older line gives way to it.
	const followUps = $derived(initiative ? null : withFollowUps(node));
</script>

{#snippet statusLine()}
	<span class="word {tone}">{label}</span>{#if reason}<span class="reason">{' · '}{localizeInstants(reason, $localDayClock)}</span>{/if}
{/snippet}

<section {id} class="node-details" style={`--indent:${indent}`} aria-label={node.title + ' details'}>
	<p class="status"><span class="prefix">Status:</span> {@render statusLine()}</p>
	{#if causes.length}
		<div class="causes">
			<span class="prefix">Why:</span>
			<!-- The named cause is sufficient; internal dependency ids stay hidden. -->
			<ul>{#each causes as cause}<li>{localizeInstants(cause.reason, $localDayClock)}</li>{/each}</ul>
		</div>
	{/if}
	{#if showTries}
		<div class="tries">
			<p><span class="prefix">Tries:</span> {execution.attempt_count}{#if execution.attempt_count > execution.attempts.length}{' '}(latest {execution.attempts.length} shown){/if}</p>
			<ul>
				{#each execution.attempts as attempt}
					<li>Try {attempt.ordinal} · {attempt.executor.label} · {tryEnding(attempt.released_at, attempt.release_reason)}</li>
				{/each}
			</ul>
		</div>
	{/if}

	{#if next}
		<p class="next"><span class="prefix">Next:</span> {next}</p>
	{/if}

	{#if showEvidence && node.evidence}
		<p class="evidence"><span class="prefix">Evidence:</span> {node.evidence.evidence}</p>
		{#each evidenceNotes as note}<p class="evidence-note note">{note}</p>{/each}
	{:else if showEvidence}
		<p class="evidence"><span class="prefix">Evidence:</span> none recorded</p>
	{/if}

	{#if working}<p class="working"><span class="prefix">Working:</span> {working}</p>{/if}
	{#if waiting}<p class="waiting"><span class="prefix">Waiting:</span> {waiting}</p>{/if}
	{#if tokenUsage}<p class="tokens"><span class="prefix">Tokens:</span> {tokenUsage}</p>{/if}
	{#if cost}<p class="cost"><span class="prefix">Cost:</span> {cost}</p>{/if}
	{#if ownWorking}<p class="own-working"><span class="prefix">This step working:</span> {ownWorking}</p>{/if}
	{#if ownWaiting}<p class="own-waiting"><span class="prefix">This step waiting:</span> {ownWaiting}</p>{/if}
	{#if ownTokens}<p class="own-tokens"><span class="prefix">This step tokens:</span> {ownTokens}</p>{/if}
	{#if ownCost}<p class="own-cost"><span class="prefix">This step cost:</span> {ownCost}</p>{/if}
	{#if initiative}
		<p class="initiative">{#each initiative.segments as segment, index}{#if index > 0}{' · '}{/if}<span class="segment">{#if index === 0}<span class="prefix">{initiative.label}</span>{' '}{/if}{segment}</span>{/each}</p>
	{/if}
	{#if followUps}<p class="follow-ups"><span class="prefix">With revisions and follow-ups:</span> {followUps}</p>{/if}
</section>

<style>
	.node-details {
		display: flex; flex-direction: column; gap: var(--space-1);
		margin: 0 var(--space-2) var(--space-2) calc(34px + var(--indent));
		padding: var(--space-1) var(--space-2); border-left: 2px solid var(--border);
		color: var(--text-secondary); font-size: var(--text-caption);
	}
	ul, p { margin: 0; }
	ul { padding-left: var(--space-3); }
	.causes > .prefix { display: block; }
	.evidence-note { margin-left: var(--space-3); }
	/* The line wraps only between its segments, so "3 follow-ups" and "5h 20m" never split. */
	.segment { white-space: nowrap; }
	.prefix, .note { color: var(--text-tertiary); }
	/* Only the status word carries tone, and red only where something failed or needs the user. */
	.word { color: var(--text-primary); font-weight: var(--fw-medium); }
	.word.failed, .word.blocked { color: var(--red); }
	.word.human-review { color: var(--amber); }
	/* Lifecycle actions stay in the row menu; details are read-only facts. */
	@media (max-width: 620px) {
		/* Match the title's icon and gap on touch as well as desktop. */
		.node-details { margin-left: calc(34px + var(--indent)); }
	}
</style>
