<script lang="ts">
	/** A signed-in app row's title on one line: "Figma — you@example.com". The app name never truncates; the account gives way first, with an ellipsis. */
	let { name, account }: { name: string; account: string } = $props();
</script>

<span class="app-account-title" title={`${name} — ${account}`}><span class="app-name">{name} —</span><span class="app-account">{account}</span></span>

<style>
	.app-account-title { display: flex; min-width: 0; gap: var(--space-1); white-space: nowrap; font-size: var(--text-subhead); color: var(--text-primary); }
	.app-name { flex: 0 0 auto; }
	.app-account { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
</style>
