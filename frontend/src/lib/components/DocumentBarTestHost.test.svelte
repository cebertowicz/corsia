<script lang="ts">
	import { documentBar } from '$lib/stores/documentBar';
</script>

<!-- Stands in for the bar that holds the canvas (the app bar or the tab strip) in tests that mount a document without it. -->
{#if $documentBar}<div data-test-document-bar>{#if $documentBar.titleEnd}{@render $documentBar.titleEnd()}{/if}{@render $documentBar.tools()}</div>{/if}
