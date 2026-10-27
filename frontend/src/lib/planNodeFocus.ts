import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeThreadId } from '$lib/stores/threads';

/**
 * Showing the active thread's plan at one node: one direct hand-off, never stored.
 *
 * Callers are an attention item and the return from a subagent leaf. A plan
 * surface (the desktop pane's Plan section, the phone's plan drawer)
 * registers how to show a thread's plan, and a mounted ThreadPlanPane
 * registers how to focus a node in its thread; that pane owns the rest,
 * including waiting for the plan to load and saying so when the node cannot
 * be shown. With no surface, no handler, or a thread the user has already
 * left, the request is dropped right here; nothing waits to fire later.
 */
type ShowPlan = (threadId: string) => boolean;
/** `stepId`: for a node of a nested subagent plan, the step of this thread's plan that nests it. */
/** `open`: also open the row's details, as pressing the row does (a person step's Do →). */
type FocusNode = (nodeId: string, stepId: string | null, open: boolean) => void;

const planSurfaces = new Set<ShowPlan>();
const focusHandlers = new Map<string, FocusNode>();

/** Register a surface that can show a thread's plan; returns its unregister. */
export function registerPlanSurface(show: ShowPlan): () => void {
	planSurfaces.add(show);
	return () => planSurfaces.delete(show);
}

/** Register the mounted plan pane that focuses nodes for one thread; returns its unregister. */
export function registerNodeFocus(threadId: string, focus: FocusNode): () => void {
	focusHandlers.set(threadId, focus);
	return () => {
		if (focusHandlers.get(threadId) === focus) focusHandlers.delete(threadId);
	};
}

/** Show the active thread's plan and hand the node to its pane, or drop the request. */
export async function focusPlanNode(
	threadId: string, nodeId: string | null, stepId: string | null = null, open = false,
): Promise<void> {
	if (!nodeId) return;
	// Let surfaces render the thread the caller just navigated to before asking them.
	await tick();
	if (get(activeThreadId) !== threadId) return;
	let shown = false;
	for (const show of planSurfaces) shown = show(threadId) || shown;
	if (!shown) return;
	await tick();
	if (get(activeThreadId) !== threadId) return;
	focusHandlers.get(threadId)?.(nodeId, stepId, open);
}
