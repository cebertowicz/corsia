import { derived, type Readable } from 'svelte/store';
import { activeModelId, models, threadCatalogs, workspaceModels } from './modelsState';
import { threadModelFor, threadModelVersion, type Model } from './models';
import { heldThreadSwitch } from './threadModelTruth';
import { unknownStatus } from './modelsLoad';
import { subagentMeta, subagentThreadRows } from './subagents';
import { activeThreadId, reasoningEffort, threads, type Thread } from './threads';

export type ComposerThreadRow = Partial<Pick<
	Thread,
	| 'model' | 'reasoning_effort' | 'project_id'
	| 'pending_model' | 'switch_status' | 'switch_error'
>>;

/** What one composer's model and effort controls read and change: its own thread's selection. */
export interface ComposerSelection {
	/** The thread the composer is bound to; null for a draft chat. */
	threadId: string | null;
	/** Whether this is the open thread (or a draft chat), whose selection lives in the global stores. */
	isOpen: boolean;
	/** The thread's row: the list's, or a worker's own (a worker is not in the list). */
	thread: ComposerThreadRow | undefined;
	/** The thread's switch state: its row's, or the one held beside the model of a thread the list omits (the conductor). */
	switching: Pick<ComposerThreadRow, 'pending_model' | 'switch_status' | 'switch_error'> | undefined;
	modelId: string | null;
	effort: string | null;
	/** The catalog read on this thread's account; a thread not yet read has every status unknown. */
	models: Model[];
}

/**
 * Resolve a composer's selection. The open thread, and a draft chat, read the global
 * selection the thread list keeps current; any other thread reads its own row.
 * An omitted id means the open thread.
 */
export const composerSelectionFor: Readable<(threadId?: string | null) => ComposerSelection> = derived(
	[activeThreadId, activeModelId, reasoningEffort, threads, subagentMeta, subagentThreadRows, threadModelVersion, models, threadCatalogs, workspaceModels],
	([$activeThreadId, $activeModelId, $reasoningEffort, $threads, $subagentMeta, $workerRows, , $models, $catalogs, $workspaceModels]) =>
		(threadId?: string | null): ComposerSelection => {
			const id = threadId ?? $activeThreadId;
			const worker = $subagentMeta.get(id ?? '');
			const thread: ComposerThreadRow | undefined = $threads.find((row) => row.id === id)
				?? (worker ? { ...$workerRows.get(worker.childThreadId), model: threadModelFor(worker.childThreadId) } : undefined);
			const switching = thread ?? heldThreadSwitch(id ?? '');
			if (id === null || id === $activeThreadId) {
				return { threadId: id, isOpen: true, thread, switching, modelId: $activeModelId, effort: $reasoningEffort, models: $models };
			}
			return {
				threadId: id, isOpen: false, thread, switching,
				modelId: thread?.model ?? null, effort: thread?.reasoning_effort ?? null,
				models: $catalogs.get(id) ?? unknownStatus($workspaceModels),
			};
		},
);
