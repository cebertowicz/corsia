<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { disconnect } from '$lib/ws';
	import { uiScope } from '$lib/stores/uiScope';
	import { paletteOpen } from '$lib/stores/palette';
	import { paneOpen } from '$lib/stores/paneOpen';
	import { paneCollapsed } from '$lib/stores/paneCollapsed';
	import { installLinkTargetResolver } from '$lib/linkTargetResolver';
	import { theme } from '$lib/stores/theme';
	import { ACCENT_ROLE_TOKENS, accentRoles, type AccentRoles } from '$lib/utils/projectColors';

	let { children } = $props();

	function onGlobalKey(e: KeyboardEvent) {
		const meta = e.metaKey || e.ctrlKey;
		if (!meta) return;
		if (e.key === 'k') { e.preventDefault(); paletteOpen.update((v) => !v); }
		else if (e.key === '\\') {
			e.preventDefault();
			if (window.innerWidth <= 1024) paneOpen.update((v) => !v); else paneCollapsed.update((v) => !v);
		}
	}

	onMount(() => {
		const removeLinkTargetResolver = installLinkTargetResolver();
		return () => {
			removeLinkTargetResolver();
			disconnect();
		};
	});

	$effect(() => {
		const s = $uiScope;
		const root = document.documentElement;
		// The project's colour is the accent and every role comes from it; without a scope, or with a
		// colour that does not parse, the tokens' defaults (the corsia project's #007faa) apply again.
		const roles = s ? accentRoles(s.color, $theme) : null;
		for (const [role, token] of Object.entries(ACCENT_ROLE_TOKENS) as [keyof AccentRoles, string][]) {
			if (roles) root.style.setProperty(token, roles[role]);
			else root.style.removeProperty(token);
		}
	});
</script>

<svelte:window onkeydown={onGlobalKey} />

{@render children()}
