<!--
	Every state in which the plan rollup has nothing to list.

	These are deliberately not collapsed into one "unavailable" message: an
	expired sign-in, a forbidden scope, a stale selection that no longer exists,
	a transport failure, and a genuinely empty scope each need a different
	action from the reader. Keeping them in one
	component keeps their shared shape from drifting apart.
-->
<script module lang="ts">
	export type ActivityScopeStateKind =
		| 'loading' | 'unauthorized' | 'forbidden' | 'not-found'
		| 'error' | 'empty';
</script>

<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';

	let { kind, scopeNoun, canGoWorkspace = false, onRetry, onGoWorkspace }: {
		kind: ActivityScopeStateKind;
		scopeNoun: 'project' | 'workspace';
		/** A project scope can hand back to Workspace; Workspace has nowhere to go. */
		canGoWorkspace?: boolean;
		onRetry: () => void;
		onGoWorkspace: () => void;
	} = $props();
</script>

{#if kind === 'loading'}
	<div class="activity-loading" aria-live="polite" aria-label="Loading activity">
		<span></span><span></span><span></span>
	</div>
{:else if kind === 'unauthorized'}
	<div class="activity-state" role="alert">
		<strong>Sign in to see Activity</strong>
		<span>Your sign-in expired before this {scopeNoun} loaded.</span>
	</div>
{:else if kind === 'forbidden'}
	<div class="activity-state" role="alert">
		<strong>Activity isn’t available in this {scopeNoun}</strong>
		<span>You don’t have access to this {scopeNoun}.</span>
	</div>
{:else if kind === 'not-found'}
	<div class="activity-state" role="alert">
		<strong>This {scopeNoun} isn’t available</strong>
		<span>Choose another {scopeNoun}.</span>
		{#if canGoWorkspace}
			<Button variant="secondary" size="sm" onclick={onGoWorkspace}>Back to Workspace</Button>
		{/if}
	</div>
{:else if kind === 'error'}
	<div class="activity-state" role="alert">
		<strong>Activity couldn’t load</strong>
		<Button variant="secondary" size="sm" onclick={onRetry}>Retry</Button>
	</div>
{:else}
	<div class="activity-state empty">
		<strong>No plans</strong>
		<span>Plans in this {scopeNoun} appear here.</span>
	</div>
{/if}

<style>
	.activity-state {
		display: flex; min-height: 132px; align-items: center; justify-content: center;
		flex-direction: column; gap: var(--space-2); color: var(--text-tertiary); text-align: center;
	}
	.activity-state strong { color: var(--text-primary); font-size: var(--text-body); }
	.activity-state span { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.activity-loading { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-3) 0; }
	.activity-loading span {
		display: block; height: 44px; border-radius: var(--radius-xs);
		background: linear-gradient(90deg, var(--bg-secondary), var(--bg-tertiary), var(--bg-secondary));
		background-size: 200% 100%; animation: activity-shimmer 1.4s ease-in-out infinite;
	}
	@keyframes activity-shimmer { to { background-position: -200% 0; } }
	@media (prefers-reduced-motion: reduce) { .activity-loading span { animation: none; } }
</style>
