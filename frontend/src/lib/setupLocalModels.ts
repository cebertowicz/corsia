import { derived, get } from 'svelte/store';
import { loadProviders, providers } from '$lib/stores/providers';
import { loadNetworkServers, networkServers } from '$lib/stores/networkServers';
import { loadProviderModelsFor, providerModels } from '$lib/stores/providerModels';
import { loadModel } from '$lib/stores/providerModelCommands';
import { canLoadAnyway } from '$lib/stores/providerModelsHost';
import type { ModelSource } from '$lib/conductorSources';

/** Read the pre-account projection; callers preserve their last result on failure. */
export async function readPreauthLocal(): Promise<ModelSource | undefined> {
 try {
  const response = await fetch('/api/setup/local-download');
  if (!response.ok) return undefined;
  const live = await response.json();
  return { source: 'omlx', name: live.model ?? 'Local model', state: live.state, local_state: live.state,
   download_percent: live.percent, download_eta_seconds: live.eta_seconds, download_size: live.download_size, download_stalled: live.stalled,
   setup_error_code: live.cause, setup_error_reference: live.ref, bytes_to_free: live.bytes_to_free, fix: live.fix };
 } catch { return undefined; }
}

/** Settings owns the server and model rows; setup reads the same live refusal. */
export const localLoadRefusal = derived([networkServers, providers, providerModels], ([$servers, $providers, $models]) => {
 const server = $servers.find((item) => item.corsia_omlx);
 const provider = $providers.find((item) => item.name === 'omlx');
 const model = server && $models[`omlx|${server.id}`]?.models.find((item) => canLoadAnyway(item, false));
 return server && provider && model ? { server, provider, model } : undefined;
});

export async function refreshSetupLocalModels(): Promise<void> {
 await Promise.all([loadNetworkServers(), loadProviders()]);
 const server = get(networkServers).find((item) => item.corsia_omlx);
 const provider = get(providers).find((item) => item.name === 'omlx');
 if (server && provider) await loadProviderModelsFor(provider, server);
}

export async function retrySetupLocalModel(): Promise<void> {
 const refusal = get(localLoadRefusal);
 if (refusal) await loadModel(refusal.provider, refusal.server, refusal.model.id);
}
