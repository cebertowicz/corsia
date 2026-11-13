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
		<span class="rl-who" role="status" aria-live="polite">
			{#if notice === 'loading'}
				<span class="rl-text">Checking who gets this…</span>
			{:else if notice === 'error'}
				<span class="rl-text">Couldn’t check who gets this.</span>
			{:else if empty}
				<span class="rl-text">{none}</span>
			{:else}
				<span class="rl-text">Goes to</span>
				{#each names as name, index (name + index)}
					{#if index > 0 && index === names.length - 1}<span class="rl-text">and</span>{/if}
					<!-- Names keep their size and are never cut: they wrap to the next line first, and one wider than the row breaks inside the name,
					     because a phone has no hover to read it in full. A comma belongs to the name before it, so it never starts a line or floats after a gap. -->
					<span class="rl-item"><span class="rl-name">{name}</span>{#if index < names.length - 2}<span class="rl-text">,</span>{/if}</span>
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
	.rl-who { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: var(--space-1); row-gap: var(--space-px-2); min-width: 0; padding-top: var(--space-px-3); font-size: var(--text-caption); color: var(--text-secondary); }
	.rl-text { flex: none; color: var(--text-secondary); white-space: nowrap; }
	.rl-item { display: flex; align-items: baseline; min-width: 0; max-width: 100%; }
	.rl-name { min-width: 0; overflow-wrap: anywhere; color: var(--text-primary); font-weight: var(--fw-semibold); }
	/* The link owns its padding and leaves room for its 2px focus ring and 2px offset, so the ring touches no text and no border beside it. */
	.rl-link { background: none; border: none; padding: var(--space-px-3) var(--space-2); font: inherit; font-size: var(--text-caption); color: var(--text-secondary); cursor: pointer; text-decoration: underline; text-underline-offset: var(--space-px-2); white-space: nowrap; border-radius: var(--radius-xs); }
	.rl-link:hover { color: var(--text-primary); }
	.rl-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* On touch the link's own box is the 44px target, so it takes real room instead of an invisible area that would cover the field above it;
	   the names sit on the link's first line. */
	/* A phone on its side has about 150px under a highlight: the line sits against the field and takes only its text's height. */
	@media (pointer: coarse) and (max-height: 28.125rem) { .rl { margin-top: 0; } .rl-row { min-height: var(--space-4); } }
	@media (pointer: coarse) {
		.rl-link { display: inline-flex; align-items: center; min-height: var(--touch-target); }
		.has-link .rl-who { padding-top: calc((var(--touch-target) - 1lh) / 2); }
	}
</style>
