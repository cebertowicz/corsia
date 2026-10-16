import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
import {
	getModelDispatchId,
	modelAvailability,
	saveThreadModel,
	type Model,
} from '$lib/stores/models';
import { fetchCorsiaAgent } from '$lib/utils/conductor';

async function readyModelForSetupSource(
	token: string,
	source: ModelSource,
	threadId: string,
): Promise<Model | null> {
	try {
		const response = await fetch(`/api/models?thread_id=${encodeURIComponent(threadId)}`, {
			headers: { Authorization: `Bearer ${token}` },
		});
		if (!response.ok) return null;
		const rows = (await response.json()).models;
		if (!Array.isArray(rows)) return null;
		return rows.find((model): model is Model => (
			model?.connection === source.source && modelAvailability(model).status === 'ready'
		)) ?? null;
	} catch {
		return null;
	}
}

/** Select the first ready setup source on the existing CORSIA-agent model path. */
export async function selectSetupFirstModel(token: string | null, sources: ModelSource[]): Promise<boolean> {
	if (!token) return false;
	const source = sources.find(sourceIsReady);
	if (!source) return false;
	const lookup = await fetchCorsiaAgent(token);
	if (lookup.kind !== 'target') return false;
	const model = await readyModelForSetupSource(token, source, lookup.target.thread_id);
	if (!model) return false;
	try {
		await saveThreadModel(lookup.target.thread_id, getModelDispatchId(model), token);
		return true;
	} catch {
		return false;
	}
}

/** Finishing setup must leave the model picker available when preselection cannot complete. */
export async function finishSetupWithBestEffortSelection(
	token: string | null,
	sources: ModelSource[],
	onFinished: () => void,
): Promise<void> {
	try {
		await selectSetupFirstModel(token, sources);
	} catch {
		// Preselection is optional; the finished app exposes the picker as recovery.
	}
	onFinished();
}
