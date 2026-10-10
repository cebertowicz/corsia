import { writable } from 'svelte/store';
import type { Model } from './models';

// The catalog stores, kept apart from `models.ts` so the loader can write them
// without importing the module that re-exports it.

/** The open thread's catalog: its harness rows read that thread's account. Composer and picker only. */
export const models = writable<Model[]>([]);

/** The unscoped catalog (the workspace Default account), for workspace-level surfaces such as Settings. */
export const workspaceModels = writable<Model[]>([]);

export const activeModelId = writable<string | null>(null);

export function getModelDispatchId(model: Model): string {
	return model.litellm_model ?? model.id;
}
