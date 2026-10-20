import { get } from 'svelte/store';
import { models, workspaceModels } from './modelsState';

/**
 * Re-filter the picker to the Local only switch's new value before its save is confirmed.
 *
 * The sheet hides rows by their `thread_local_only` cause, so the preview sets or clears that
 * cause on the catalog it already holds. Returns the restore for a failed save; a confirmed one
 * reads the thread's catalog again.
 */
export function previewLocalOnly(next: boolean): () => void {
	const before = get(models);
	const unscoped = get(workspaceModels);
	models.set(next
		? before.map((model) => model.is_local === true ? model : { ...model, status: 'unavailable', status_reason: 'thread_local_only' })
		: before.map((model) => model.status_reason === 'thread_local_only' ? unscoped.find((row) => row.id === model.id) ?? model : model));
	return () => models.set(before);
}
