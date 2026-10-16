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
	import { accentRoles } from '$lib/utils/projectColors';

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
		const keys = ['--accent', '--accent-hover', '--violet', '--violet-2', '--violet-text', '--violet-bg', '--violet-line', '--accent-bg', '--accent-subtle', '--on-accent'];
		if (s) {
			// A project's colour fills as itself; its text and the ink on its fill are derived so both read.
			const roles = accentRoles(s.color, $theme);
			root.style.setProperty('--accent', s.color);
			root.style.setProperty('--accent-hover', s.color);
			root.style.setProperty('--violet', s.color);
			root.style.setProperty('--violet-2', s.color);
			root.style.setProperty('--violet-text', roles.text);
			root.style.setProperty('--on-accent', roles.onFill);
			root.style.setProperty('--violet-bg', `color-mix(in srgb, ${s.color} 12%, transparent)`);
			root.style.setProperty('--violet-line', `color-mix(in srgb, ${s.color} 30%, transparent)`);
			root.style.setProperty('--accent-bg', `color-mix(in srgb, ${s.color} 10%, transparent)`);
			root.style.setProperty('--accent-subtle', `color-mix(in srgb, ${s.color} 12%, transparent)`);
		} else {
			keys.forEach((k) => root.style.removeProperty(k));
		}
	});
</script>

<svelte:window onkeydown={onGlobalKey} />

{@render children()}
