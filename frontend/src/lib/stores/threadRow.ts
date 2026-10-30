import { patchSubagentThread, type SubagentThreadFields } from './subagents';
import { patchHeldSwitch } from './threadModelTruth';
import { threads } from './threads';

/** The one way to change a thread's row fields: the list holds a chat's row, a worker's lives beside the registry, the scope's conductor's switch state beside its model. */
export function updateThreadRow(threadId: string, fields: SubagentThreadFields): void {
	threads.update((rows) => rows.map((row) => (row.id === threadId ? { ...row, ...fields } : row)));
	patchSubagentThread(threadId, fields);
	patchHeldSwitch(threadId, fields);
}
