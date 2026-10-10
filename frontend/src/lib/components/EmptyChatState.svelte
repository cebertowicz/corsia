<script lang="ts">
	import { fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { user } from '$lib/stores/auth';
	import { uiScope } from '$lib/stores/uiScope';
	import { projects, enterProjectScope } from '$lib/stores/projects';
	import { projectAccent } from '$lib/utils/projectColors';
	import { paletteOpen } from '$lib/stores/palette';

	/** The opening screen of an empty thread. Presentational plus navigation. */
	const homeProjects = $derived($projects.filter((p) => p.status === 'active'));
</script>

<div class="empty-chat" in:fade={{ duration: 280, easing: cubicOut }}>
	<img class="empty-mark" src="/icons/corsia-mark.svg" alt="CORSIA" />
	<h2>Hey{$user?.name ? ', ' + $user.name : ''}.</h2>
	<p>What's on your mind?</p>
	{#if !$uiScope && homeProjects.length > 0}
		<div class="home-projects">
			<div class="home-projects-cap">Your projects</div>
			<div class="home-project-pills">
				{#each homeProjects as p (p.id)}
					<button class="proj-pill" onclick={() => enterProjectScope(p)}>
						<span class="proj-dot" style="background: {projectAccent(p.id, p.accent_color)};"></span>{p.name}
					</button>
				{/each}
				<button class="proj-pill proj-all" onclick={() => paletteOpen.set(true)}>All →</button>
			</div>
			<div class="home-hint">Type below to start, tap a project to dive in, or press <kbd>⌘K</kbd> to jump to anything.</div>
		</div>
	{/if}
</div>

<style>
	.empty-chat {
		height: 100%; display: flex; flex-direction: column;
		align-items: center; justify-content: center; gap: 12px;
	}
	.empty-mark {
		width: 144px; height: 144px;
		opacity: 0.98;
		user-select: none;
		-webkit-user-drag: none;
	}
	:global(html[data-theme="light"]) .empty-mark {
		filter: contrast(1.08) saturate(1.18);
	}
	.empty-chat h2 { font-size: var(--text-title2); font-weight: 500; color: var(--text-secondary); }
	.empty-chat p { font-size: var(--text-callout); color: var(--text-tertiary); }

	.home-projects {
		margin-top: 26px; width: 100%; max-width: 540px;
		display: flex; flex-direction: column; align-items: center; gap: 12px;
	}
	.home-projects-cap {
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.10em;
		text-transform: uppercase; color: var(--text-tertiary);
	}
	.home-project-pills { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
	.proj-pill {
		display: inline-flex; align-items: center; gap: 7px;
		padding: 6px 13px; border-radius: var(--radius-pill);
		font-size: var(--text-subhead); font-weight: 500; color: var(--text-primary);
		background: var(--bg-secondary); border: 1px solid var(--border);
		cursor: pointer; font-family: inherit; transition: background var(--dur-tight) ease, border-color var(--dur-tight) ease;
	}
	.proj-pill:hover { background: var(--bg-hover); }
	.proj-dot { width: 7px; height: 7px; border-radius: var(--radius-circle); flex-shrink: 0; }
	.proj-all { border: none; color: var(--text-tertiary); }
	.home-hint {
		margin-top: 6px; font-size: var(--text-footnote); color: var(--text-tertiary);
		max-width: 460px; text-align: center; line-height: 1.6;
	}
	.home-hint kbd {
		font-family: var(--font-mono, 'SF Mono', monospace); font-size: var(--text-footnote);
		background: var(--bg-tertiary); border: 1px solid var(--border);
		border-radius: var(--radius-2xs); padding: 1px 5px;
	}

	@media (max-width: 768px) {
		.empty-mark {
			width: 120px; height: 120px;
		}
	}
</style>
