<script lang="ts">
	import { WELCOME_ACCOUNT_LINE, WELCOME_LINES, WELCOME_ROWS } from '$lib/setupWelcome';
</script>

<div class="welcome">
	{#each WELCOME_LINES as line}<p class="line">{line}</p>{/each}
	<ul class="rows">
		{#each WELCOME_ROWS as row}<li><strong>{row.label}</strong> {row.text}</li>{/each}
	</ul>
	<p class="account">{WELCOME_ACCOUNT_LINE}</p>
</div>

<style>
	.welcome { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-normal); text-wrap: pretty; }
	p, ul { margin: 0; }
	.rows { list-style: none; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
	.rows strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.account { color: var(--text-primary); }
</style>
