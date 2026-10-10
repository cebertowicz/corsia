import type { ProviderEntry } from './providers';
import type { ModelState, ProviderModel } from './providerModels';
import { capabilities, checkpointsBelowProvider, ctxAndSizeMeta, dedupNetworkModels, humanCtx } from './providerModelUtils';

export function fallbackCloudModels(modelNames: ProviderEntry['models']): ProviderModel[] {
	return (modelNames ?? []).map((name) => ({
		id: name,
		name,
		state: 'default' as ModelState,
		meta: '',
		caps: [],
	}));
}

export function cloudProviderModels(data: unknown): ProviderModel[] {
	const items: any[] = (data as { models?: any[] })?.models ?? (Array.isArray(data) ? data : []);
	return items.map((model) => {
		const caps: ProviderModel['caps'] = [];
		if (model.supports_vision) caps.push('vision');
		if (model.supports_reasoning) caps.push('reasoning');
		if (model.supports_tools) caps.push('tools');
		const ctx = model.context_window ? humanCtx(model.context_window) : '';
		const price = (model.input_cost_per_1m != null && model.output_cost_per_1m != null)
			? `$${model.input_cost_per_1m}/$${model.output_cost_per_1m} per M`
			: '';
		return {
			id: model.db_id ?? model.id,
			name: model.display_name ?? model.id,
			state: model.enabled ? 'loaded' as ModelState : 'default' as ModelState,
			meta: [ctx, price].filter(Boolean).join(' · '),
			caps,
			contextLength: model.context_window,
			dbId: model.db_id ?? model.id,
		};
	});
}

export function networkProviderModels(data: any): ProviderModel[] {
	const modelMeta = (model: any, sizeBytes?: number) => {
		const providerWindow = model.provider_context_length ?? model.context_length;
		const checkpointWindow = checkpointsBelowProvider(model) ? model.context_length : undefined;
		return ctxAndSizeMeta(providerWindow, sizeBytes, model.quantization, checkpointWindow);
	};
	const loaded = (data.loaded ?? []).map((model: any) => {
		const id = model.model_id ?? model.id ?? model.identifier ?? '';
		const sizeBytes = model.memory_gb ? model.memory_gb * (1024 ** 3) : model.size_bytes;
		return {
			id,
			name: model.display_name ?? model.model_alias ?? id,
			state: 'loaded' as ModelState,
			meta: model.unusable_reason ?? modelMeta(model, sizeBytes),
			caps: capabilities(model),
			contextLength: model.context_length,
			providerContextLength: model.provider_context_length,
			sizeBytes,
			dbId: model.db_id,
			imageInput: model.image_input ?? null,
		};
	});
	const available = (data.available ?? []).map((model: any) => {
		const id = model.model_id ?? model.repo_id ?? model.id ?? model.identifier ?? '';
		return {
			id,
			name: model.display_name ?? model.model_alias ?? id,
			state: 'default' as ModelState,
			meta: model.unusable_reason ?? modelMeta(model, model.size_bytes),
			caps: capabilities(model),
			contextLength: model.context_length,
			providerContextLength: model.provider_context_length,
			sizeBytes: model.size_bytes,
			dbId: model.db_id,
			imageInput: model.image_input ?? null,
		};
	});
	return dedupNetworkModels([...loaded, ...available]);
}
