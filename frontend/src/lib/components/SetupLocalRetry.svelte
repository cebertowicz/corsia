<script lang="ts">
 import { get } from 'svelte/store';
 import { localLoadRefusal, retrySetupLocalModel, type SetupRefusal } from '$lib/setupLocalModels';
 import type { PreloadInfo } from '$lib/stores/providerModelsHost';
 import PreLoadCheckModal from './v7/PreLoadCheckModal.svelte';

 let { target = $bindable<SetupRefusal | null>(null), busy = $bindable(false), error = $bindable(''), refresh }: {
  target: SetupRefusal | null; busy: boolean; error: string; refresh: () => Promise<void>;
 } = $props();
 let preloadInfo = $state<PreloadInfo | null>(null);

 export async function retry() {
  const next = get(localLoadRefusal) ?? target;
  if (busy || !next) return;
  target = next; error = ''; busy = true;
  try {
   const result = await retrySetupLocalModel(next, (info) => { preloadInfo = info; });
   error = result.error;
   await refresh();
   if (result.loaded) target = null;
  } finally { busy = false; }
 }
</script>

{#if preloadInfo}<PreLoadCheckModal {...preloadInfo} />{/if}
