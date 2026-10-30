import { derived, get, writable } from 'svelte/store';
import { loadProviders, providers } from '$lib/stores/providers';
import { loadNetworkServers, networkServers } from '$lib/stores/networkServers';
import { loadProviderModelsFor, providerModels } from '$lib/stores/providerModels';
import { token } from '$lib/stores/auth';
import { canLoadAnyway, checkAndLoad, type PreloadInfo } from '$lib/stores/providerModelsHost';
import { loadModel } from '$lib/stores/providerModelCommands';
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

/** The setup flight identifies the model; never choose an unrelated refused row. */
const setupModelId = writable<string | null>(null);
export const localLoadRefusal = derived([networkServers, providers, providerModels, setupModelId], ([$servers, $providers, $models, $id]) => {
 const server = $servers.find((item) => item.corsia_omlx);
 const provider = $providers.find((item) => item.name === 'omlx');
 const model = server && $id && $models[`omlx|${server.id}`]?.models.find((item) => item.id === $id && canLoadAnyway(item, false));
 return server && provider && model ? { server, provider, model } : undefined;
});

export function currentSetupRefusal() { return get(localLoadRefusal); }
export type SetupRefusal = NonNullable<ReturnType<typeof currentSetupRefusal>>;

/** Use Settings' load action and memory confirmation, preserving the returned error. */
export async function retrySetupLocalModel(target: SetupRefusal, show: (info: PreloadInfo | null) => void) {
 let error = '';
 const loaded = await checkAndLoad(target.model, true, get(token) || '', target.provider.name, async () => {
  const result = await loadModel(target.provider, target.server, target.model.id);
  if (!result.ok) error = typeof result.message === 'string' ? result.message : '';
  return result.ok;
 }, show);
 return { loaded, error };
}

export async function refreshSetupLocalModels(): Promise<void> {
 const session = get(token);
 const flight = session ? await fetch('/api/providers/local/setup', { headers: { Authorization: `Bearer ${session}` } }).then((r) => r.ok ? r.json() : null).catch(() => null) : null;
 setupModelId.set(typeof flight?.model === 'string' ? flight.model.split('/').pop() ?? null : null);
 await Promise.all([loadNetworkServers(), loadProviders()]);
 const server = get(networkServers).find((item) => item.corsia_omlx);
 const provider = get(providers).find((item) => item.name === 'omlx');
 if (server && provider) await loadProviderModelsFor(provider, server);
}
