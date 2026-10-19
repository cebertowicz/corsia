import { getModelDispatchId, saveThreadModel, type Model } from '$lib/stores/models';
import { fetchCorsiaAgent } from '$lib/utils/conductor';

export type SetupModel = Model & { is_default?: boolean };
export type TestedProvider = { source: string; model?: string };
export type SetupBinding = { model: string; source: string };

/** Setup and Start share the catalog's readiness, including the selected account. */
export function setupModelWorks(model: SetupModel): boolean {
	if (model.status !== 'ready') return false;
	if (model.connection === 'claude-code' || model.connection === 'codex') {
		return model.runs_on?.find((account) => account.selected)?.status === 'ready';
	}
	return true;
}

/** Prefer each subscription's advertised default, then a tested API provider’s default (or first ready row), then local. */
export function bestSetupModel(models: SetupModel[], tested: TestedProvider[] = []): SetupModel | undefined {
	const ready = models.filter(setupModelWorks);
	for (const connection of ['claude-code', 'codex']) {
		const rows = ready.filter((row) => row.connection === connection);
		const model = rows.find((row) => row.is_default) ?? rows[0];
		if (model) return model;
	}
	for (const test of tested) {
		const rows = ready.filter((row) => row.connection === test.source || row.provider === test.source);
		const model = rows.find((row) => row.is_default) ?? rows[0];
		if (model) return model;
	}
	return ready.find((row) => row.connection === 'omlx');
}

/** Read the same scope the CORSIA agent will use, never the unscoped catalog. */
export async function setupCatalog(session: string): Promise<{ models: SetupModel[]; thread: string }> {
	const lookup = await fetchCorsiaAgent(session);
	if (lookup.kind !== 'target') throw new Error('CORSIA agent unavailable');
	const thread = lookup.target.thread_id;
	const response = await fetch(`/api/models?thread_id=${encodeURIComponent(thread)}`, { headers: { Authorization: `Bearer ${session}` } });
	if (!response.ok) throw new Error(`model catalog ${response.status}`);
	const body = await response.json();
	if (!Array.isArray(body.models)) throw new Error('model catalog unavailable');
	return { models: body.models, thread };
}

/** Refresh and bind before completion; failures leave setup open. */
export async function bindSetupFirstModel(session: string, tested: TestedProvider[] = [], beforeBind?: (binding: SetupBinding) => Promise<void>): Promise<SetupBinding> {
	const { models, thread } = await setupCatalog(session);
	const model = bestSetupModel(models, tested);
	if (!model) throw new Error('No working setup model');
	const selector = getModelDispatchId(model);
	const binding = { model: selector, source: model.connection ?? model.provider ?? '' };
	await beforeBind?.(binding);
	await saveThreadModel(thread, selector, session, 'setup');
	return binding;
}
