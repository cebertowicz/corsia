<script lang="ts">
	import { WELCOME_ACCOUNT_LINE, WELCOME_LINES } from '$lib/setupWelcome';

	/** From 1280 wide the account sentence is the form's lede, so the beside-the-form column leaves it out. */
	const { accountLine = true }: { accountLine?: boolean } = $props();
</script>

<div class="welcome">
	<img class="mark" src="/icons/corsia-mark.svg" alt="CORSIA" />
	<p class="line">{WELCOME_LINES.join(' ')}</p>
	{#if accountLine}<p class="account">{WELCOME_ACCOUNT_LINE}</p>{/if}
</div>

<style>
	.welcome { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-normal); text-wrap: pretty; }
	p { margin: 0; }
	.mark { width: calc(var(--space-8) * 3); height: calc(var(--space-8) * 3); }
	.account { color: var(--text-primary); }
</style>
