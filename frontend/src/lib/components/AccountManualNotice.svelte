<script lang="ts">
	import type { ConnectedAccount } from '$lib/api/connectedApps';

	let { account, removed = false, onDismiss }: { account: ConnectedAccount; removed?: boolean; onDismiss?: () => void } = $props();
	const microsoftPermissionsUrl = (account: ConnectedAccount) => account.account_kind === 'personal' ? 'https://account.live.com/consent/Manage' : 'https://myapps.microsoft.com';
</script>

{#if account.provider === 'microsoft'}
	<p class:manual-removal={removed} class:impact={!removed} role="status">Microsoft may still list CORSIA as allowed for {account.address}. <a href={microsoftPermissionsUrl(account)} target="_blank" rel="noopener" onclick={onDismiss}>Manage Microsoft permissions</a></p>
{:else if account.provider === 'google' && removed}
	<p class="manual-removal" role="status">CORSIA removed {account.provider_display_name} — {account.address} here, but couldn’t reach Google. To remove it there, open your <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">Google Account</a> and remove CORSIA.</p>
{:else if account.provider === 'google'}
	<p class="impact" role="status">You’re signed out here, but CORSIA couldn’t reach Google, so Google may still list CORSIA as allowed. To remove it, open your <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">Google Account</a> and remove CORSIA.</p>
{/if}

<style>
	.manual-removal { margin: var(--space-3) var(--space-2) 0; color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.impact { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.manual-removal a, .impact a { color: var(--accent-text); text-decoration: none; }
	.manual-removal a:hover, .impact a:hover { text-decoration: underline; }
</style>
