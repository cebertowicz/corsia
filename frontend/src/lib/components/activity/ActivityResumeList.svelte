<!--
	The Continue rail: the threads to pick back up.

	Ordering is recency alone (see `activityHome.buildResumeThreads`); the
	activity word is a mark, not a rank, so a thread never jumps the queue for
	having unread output.
-->
<script lang="ts">
	import ActivityIdentityGlyph from './ActivityIdentityGlyph.svelte';
	import type { ResumeThread } from '$lib/activityHome';

	let { threads, projectName, onSelectThread }: {
		threads: ResumeThread[];
		/** Resolved project name, or null while the catalog has not supplied one. */
		projectName: (id: string | null) => string | null;
		onSelectThread: (threadId: string) => void | Promise<void>;
	} = $props();

	function activityLabel(value: 'active' | 'done' | null): string {
		if (value === 'active') return 'In progress';
		if (value === 'done') return 'New activity';
		return 'Recent';
	}
</script>

{#if threads.length > 0}
	<div class="resume-list">
		{#each threads as thread (thread.id)}
			<button class="resume-row" onclick={() => onSelectThread(thread.id)}>
				<span class="resume-mark" aria-hidden="true"><ActivityIdentityGlyph kind={thread.ownerType} /></span>
				<span class="resume-copy">
					<strong>{thread.title}</strong>
					<span>
						{thread.ownerType === 'agent' ? 'Agent' : 'Thread'}
						{#if projectName(thread.projectId)} · {projectName(thread.projectId)}{/if}
					</span>
				</span>
				<span class="resume-state {thread.activity ?? 'recent'}">
					{activityLabel(thread.activity)}
				</span>
				<span class="resume-chevron" aria-hidden="true">›</span>
			</button>
		{/each}
	</div>
{:else}
	<p class="quiet-empty">Your recent chats will appear here.</p>
{/if}

<style>
	.resume-list { display: flex; flex-direction: column; }
	.resume-row {
		display: flex; align-items: center; gap: var(--space-2); min-height: 56px; width: 100%;
		padding: var(--space-2) 0; border: 0; border-bottom: 1px solid var(--border-subtle);
		background: transparent; color: inherit; text-align: left; cursor: pointer;
	}
	.resume-row:last-child { border-bottom: 0; }
	/* Pointer-only: a tap leaves :hover stuck on the last row touched. */
	@media (hover: hover) {
		.resume-row:hover .resume-copy strong { color: var(--accent-text); }
	}
	.resume-row:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs);
	}
	.resume-mark {
		display: inline-flex; width: 32px; height: 32px; flex: none; align-items: center; justify-content: center;
		border-radius: var(--radius-circle); background: var(--bg-secondary); color: var(--text-secondary);
		font-size: var(--text-caption); font-weight: var(--fw-semibold);
	}
	.resume-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; }
	.resume-copy strong {
		overflow: hidden; color: var(--text-primary); font-size: var(--text-body);
		font-weight: var(--fw-medium); text-overflow: ellipsis; white-space: nowrap;
	}
	.resume-copy span { color: var(--text-tertiary); font-size: var(--text-caption); }
	.resume-state { flex: none; color: var(--text-tertiary); font-size: var(--text-caption); }
	.resume-state.active { color: var(--status-in-progress); }
	.resume-state.done { color: var(--purple-text); }
	.resume-chevron { color: var(--text-tertiary); font-size: 20px; }
	.quiet-empty { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	@media (max-width: 620px) {
		.resume-row { min-height: 44px; }
		.resume-state { display: none; }
	}
</style>
