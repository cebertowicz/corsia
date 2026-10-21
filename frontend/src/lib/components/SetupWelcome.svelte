<script lang="ts">
	import { WELCOME_ACCOUNT_LINE, WELCOME_LINES, WELCOME_ROWS } from '$lib/setupWelcome';

	/** From 1280 wide the account sentence is the form's lede, so the beside-the-form column leaves it out. */
	const { accountLine = true }: { accountLine?: boolean } = $props();
</script>

<div class="welcome">
	{#each WELCOME_LINES as line}<p class="line">{line}</p>{/each}
	<ul class="rows">
		{#each WELCOME_ROWS as row}<li><strong>{row.label}</strong> {row.text}</li>{/each}
	</ul>
	{#if accountLine}<p class="account">{WELCOME_ACCOUNT_LINE}</p>{/if}
</div>

<style>
	.welcome { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-normal); text-wrap: pretty; }
	p, ul { margin: 0; }
	ul { display: flex; flex-direction: column; gap: var(--space-2); padding-left: var(--space-5); }
	.account { color: var(--text-primary); }
</style>
