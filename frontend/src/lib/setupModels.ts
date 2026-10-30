import type { Model } from '$lib/stores/models';
import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { sourceIsReady, type ModelSource } from '$lib/conductorSources';

export type SetupModel = Model & { is_default?: boolean };
export type TestedProvider = { source: string };

/** Setup and Start share the catalog's readiness, including the selected account. */
export function setupModelWorks(model: SetupModel): boolean {
	if (model.status !== 'ready') return false;
	if (model.connection === 'claude-code' || model.connection === 'codex') {
		return model.runs_on?.find((account) => account.selected)?.status === 'ready';
	}
	return true;
}

/** True when a subscription, a tested API provider or the local model is ready to answer. Setup only reports this; the person picks the model. */
export function hasReadySetupModel(models: SetupModel[], tested: TestedProvider[] = []): boolean {
	return models.some((row) => setupModelWorks(row) && (
		row.connection === 'claude-code' || row.connection === 'codex' || row.connection === 'omlx'
		|| tested.some((test) => row.connection === test.source || row.provider === test.source)
	));
}

/** A ready source counts only when the scoped catalog offers its exact server. */
export function readySetupEndpoint(sources: ModelSource[], models: SetupModel[]): ModelSource | undefined {
	return sources.find((source) => source.source.startsWith('external:') && sourceIsReady(source)
		&& models.some((row) => row.litellm_model?.startsWith(`external@${source.source.slice('external:'.length)}/`) && setupModelWorks(row)));
}

/** Read the same scope the CORSIA agent will use, never the unscoped catalog. */
export async function setupCatalog(session: string): Promise<SetupModel[]> {
	const lookup = await fetchCorsiaAgent(session);
	if (lookup.kind !== 'target') throw new Error('CORSIA agent unavailable');
	const response = await fetch(`/api/models?thread_id=${encodeURIComponent(lookup.target.thread_id)}`, { headers: { Authorization: `Bearer ${session}` } });
	if (!response.ok) throw new Error(`Model catalog returned ${response.status}`);
	const body: unknown = await response.json();
	if (!body || typeof body !== 'object' || !Array.isArray((body as { models?: unknown }).models)) throw new Error('Model catalog unreadable');
	return (body as { models: SetupModel[] }).models;
}
