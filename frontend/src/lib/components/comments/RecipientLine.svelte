<script lang="ts">
	/** The line under a comment or reply box that names who will get it, so the person can see it before sending.
	 *  `names` are the agents; none means nobody can receive. `none` is that state's own sentence. */
	let { names, none = '', notice = null, onRetry }: {
		names: string[];
		none?: string;
		/** Who gets it is not known yet (`loading`) or could not be read (`error`): the line says so in its own place. */
		notice?: 'loading' | 'error' | null;
		onRetry?: () => void;
	} = $props();
	const empty = $derived(names.length === 0);
</script>

<div class="rl">
	<div class="rl-row" class:has-link={notice === 'error'}>
		<span class="rl-who" role="status" aria-live="polite" title={notice === null && !empty ? names.join(', ') : undefined}>
			{#if notice === 'loading'}
				<span class="rl-text">Checking who gets this…</span>
			{:else if notice === 'error'}
				<span class="rl-text">Couldn’t check who gets this.</span>
			{:else if empty}
				<span class="rl-text">{none}</span>
			{:else}
				<!-- One run of text, so a name wraps like any words, a comma follows its name with nothing between, and a screen reader hears "Docs lane, Agent and Release". -->
				<span class="rl-text">Goes to</span>
				{#each names as name, index (name + index)}
					{' '}{#if index > 0 && index === names.length - 1}<span class="rl-text">and</span>{' '}{/if}<span class="rl-name">{name}</span>{#if index < names.length - 2}<span class="rl-text">,</span>{/if}
				{/each}
			{/if}
		</span>
		{#if notice === 'error'}
			<button type="button" class="rl-link" onclick={(event) => { event.stopPropagation(); onRetry?.(); }}>Retry</button>
		{/if}
	</div>
</div>

<style>
	.rl { margin-top: var(--space-1); }
	/* Two columns: the names wrap inside the first, Retry (when the check failed) stays at the top of the second. */
	.rl-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; column-gap: var(--space-2); min-height: var(--space-5); }
	/* Names are never cut: they wrap to the next line, and one wider than the row breaks inside the name, because a phone has no hover to read it in full. */
	.rl-who { display: block; min-width: 0; padding-top: var(--space-px-3); font-size: var(--text-caption); color: var(--text-secondary); overflow-wrap: anywhere; }
	.rl-name { color: var(--text-primary); font-weight: var(--fw-semibold); }
	/* The link owns its padding and leaves room for its 2px focus ring and 2px offset, so the ring touches no text and no border beside it. */
	.rl-link { background: none; border: none; padding: var(--space-px-3) var(--space-2); font: inherit; font-size: var(--text-caption); color: var(--text-secondary); cursor: pointer; text-decoration: underline; text-underline-offset: var(--space-px-2); white-space: nowrap; border-radius: var(--radius-xs); }
	.rl-link:hover { color: var(--text-primary); }
	.rl-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* A short window (a phone on its side, a short desktop window) has no room for a second row under a reply that already scrolls: the line sits against the field on one row and ends in a single ellipsis, its full text in the hover title. */
	@media (max-height: 28.125rem) {
		.rl { margin-top: 0; }
		.rl-row { min-height: var(--space-4); }
		.rl-who { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	}
	/* On touch the link's own box is the 44px target, so it takes real room instead of an invisible area that would cover the field above it;
	   the names sit on the link's first line. */
	@media (pointer: coarse) {
		.rl-link { display: inline-flex; align-items: center; min-height: var(--touch-target); }
		.has-link .rl-who { padding-top: calc((var(--touch-target) - 1lh) / 2); }
	}
</style>
