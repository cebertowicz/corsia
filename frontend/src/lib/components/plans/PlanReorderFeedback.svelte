<script lang="ts">
 let { error, detail, announcement, dismiss }: { error: string | null; detail: string | null; announcement: string; dismiss: () => void } = $props();
</script>

{#if error}
 <div class="reorder-error" role="alert">
  <span>{error}</span>
  {#if detail}<details><summary>Details</summary>{detail}</details>{/if}
  <button type="button" onclick={dismiss}>Dismiss</button>
 </div>
{/if}
<div class="sr-only" aria-live="polite">{announcement}</div>

<style>
 .reorder-error { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; color: var(--red-text); }
 .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
