<script lang="ts">
	import { formatDateTime } from '$lib/time';
	let { msg }: { msg: { content: string; metadata: Record<string, unknown> } } = $props();
	const metadata = $derived(msg.metadata);
	const limited = $derived(metadata.reason === 'account_limit');
	const reset = $derived(limited && typeof metadata.resets_at === 'string' ? formatDateTime(metadata.resets_at) : '');
</script>

<details class="account-switch">
	<summary>{msg.content}</summary>
	<div class="details">
		{#if limited}<span>Account limit</span>{/if}
		<span>{metadata.from_account_name} → {metadata.to_account_name}</span>
		{#if reset}<span>Source resets {reset}</span>{/if}
	</div>
</details>

<style>
	.account-switch { margin: 4px auto; width: fit-content; max-width: 100%; border-top: 1px solid var(--border-subtle); border-bottom: 1px solid var(--border-subtle); padding: 8px 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	summary { cursor: pointer; color: var(--text-primary); }
	summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
	.details { display: flex; flex-wrap: wrap; gap: 6px 12px; padding-top: 8px; }
</style>
