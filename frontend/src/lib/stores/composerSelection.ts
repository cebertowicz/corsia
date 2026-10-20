import { derived, type Readable } from 'svelte/store';
import { activeModelId } from './modelsState';
import { threadModelFor } from './models';
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
	modelId: string | null;
	effort: string | null;
}

/**
 * Resolve a composer's selection. The open thread, and a draft chat, read the global
 * selection the thread list keeps current; any other thread reads its own row.
 * An omitted id means the open thread.
 */
export const composerSelectionFor: Readable<(threadId?: string | null) => ComposerSelection> = derived(
	[activeThreadId, activeModelId, reasoningEffort, threads, subagentMeta, subagentThreadRows],
	([$activeThreadId, $activeModelId, $reasoningEffort, $threads, $subagentMeta, $workerRows]) =>
		(threadId?: string | null): ComposerSelection => {
			const id = threadId ?? $activeThreadId;
			const worker = $subagentMeta.get(id ?? '');
			const thread: ComposerThreadRow | undefined = $threads.find((row) => row.id === id)
				?? (worker ? { ...$workerRows.get(worker.childThreadId), model: threadModelFor(worker.childThreadId) } : undefined);
			if (id === null || id === $activeThreadId) {
				return { threadId: id, isOpen: true, thread, modelId: $activeModelId, effort: $reasoningEffort };
			}
			return {
				threadId: id, isOpen: false, thread,
				modelId: thread?.model ?? null, effort: thread?.reasoning_effort ?? null,
			};
		},
);
