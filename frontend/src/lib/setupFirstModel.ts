import { getModelDispatchId, saveThreadModel, type Model } from '$lib/stores/models';
import { fetchCorsiaAgent } from '$lib/utils/conductor';

export type SetupModel = Model & { is_default?: boolean };
export type TestedProvider = { source: string; model?: string };
export type SetupBinding = { model: string; source: string };
export type SetupFirstModelErrorCode = 'no_model' | 'binding_incomplete' | 'catalog_refused' | 'catalog_unavailable' | 'agent_unavailable' | 'progress_save';

/** Coded setup failures keep recovery copy independent of transport wording. */
export class SetupFirstModelError extends Error {
	constructor(readonly code: SetupFirstModelErrorCode) {
		super(code);
		this.name = 'SetupFirstModelError';
	}
}

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
	let lookup: Awaited<ReturnType<typeof fetchCorsiaAgent>>;
	try {
		lookup = await fetchCorsiaAgent(session);
	} catch {
		throw new SetupFirstModelError('agent_unavailable');
	}
	if (lookup.kind !== 'target') throw new SetupFirstModelError('agent_unavailable');
	const thread = lookup.target.thread_id;
	let response: Response;
	try {
		response = await fetch(`/api/models?thread_id=${encodeURIComponent(thread)}`, { headers: { Authorization: `Bearer ${session}` } });
	} catch {
		throw new SetupFirstModelError('catalog_unavailable');
	}
	if (!response.ok) {
		throw new SetupFirstModelError(response.status >= 400 && response.status < 500 ? 'catalog_refused' : 'catalog_unavailable');
	}
	let body: unknown;
	try {
		body = await response.json();
	} catch {
		throw new SetupFirstModelError('catalog_unavailable');
	}
	if (!body || typeof body !== 'object' || !Array.isArray((body as { models?: unknown }).models)) throw new SetupFirstModelError('catalog_unavailable');
	return { models: (body as { models: SetupModel[] }).models, thread };
}

/** Refresh and bind before completion; failures leave setup open. */
export async function bindSetupFirstModel(session: string, tested: TestedProvider[] = [], beforeBind?: (binding: SetupBinding) => Promise<void>): Promise<SetupBinding> {
	const { models, thread } = await setupCatalog(session);
	const model = bestSetupModel(models, tested);
	if (!model) throw new SetupFirstModelError('no_model');
	const selector = getModelDispatchId(model);
	const binding = { model: selector, source: model.connection ?? model.provider ?? '' };
	try {
		await beforeBind?.(binding);
		const saved = await saveThreadModel(thread, selector, session, 'setup');
		if (saved.kind !== 'committed' || saved.model !== selector) throw new SetupFirstModelError('binding_incomplete');
		return binding;
	} catch (error) {
		if (error instanceof SetupFirstModelError) throw error;
		throw new SetupFirstModelError('binding_incomplete');
	}
}
