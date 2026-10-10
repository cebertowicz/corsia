import { get } from 'svelte/store';
import { openSubagentConversationTab } from './stores/artifactWorkspace';
import { token } from './stores/auth';
import {
	hydrateSubagentThread, openSubagentRequest, resolveSubagentOpenTarget, subagentMeta, type SubagentRef,
} from './stores/subagents';

export interface SubagentOpenDeps {
	readMeta: () => ReadonlyMap<string, SubagentRef>;
	/** Re-read the thread from the server; records a subagent's parent in the meta map. */
	hydrate: (threadId: string) => Promise<unknown>;
	selectThread: (threadId: string) => Promise<void> | void;
	openLeaf: (parentThreadId: string, tab: { threadId: string; title: string }) => void;
}

/**
 * Routes a request to open a thread from a chip or an attribution (#1949).
 *
 * A worker opens as a leaf beside its parent, whether the parent is already
 * known here or the server names it. Leaf placement keys on the server's
 * subagent verdict, never on a parent link alone, so a visible thread with a
 * lineage parent still opens normally. Anything else opens as it always has.
 */
export async function routeSubagentOpen(threadId: string, deps: SubagentOpenDeps): Promise<void> {
	let target = resolveSubagentOpenTarget(threadId, deps.readMeta());
	if (!target) {
		await deps.hydrate(threadId);
		target = resolveSubagentOpenTarget(threadId, deps.readMeta());
	}
	if (target) {
		void deps.selectThread(target.parentThreadId);
		deps.openLeaf(target.parentThreadId, { threadId: target.childThreadId, title: target.title });
		return;
	}
	await deps.selectThread(threadId);
}

/** Serve every `openSubagentRequest` through `routeSubagentOpen`; returns the unsubscribe. */
export function watchSubagentOpenRequests(
	page: Pick<SubagentOpenDeps, 'selectThread'>,
): () => void {
	return openSubagentRequest.subscribe((threadId) => {
		if (!threadId) return;
		openSubagentRequest.set(null);
		void routeSubagentOpen(threadId, {
			...page,
			readMeta: () => get(subagentMeta),
			hydrate: (id) => hydrateSubagentThread(id, get(token) ?? ''),
			openLeaf: openSubagentConversationTab,
		});
	});
}
