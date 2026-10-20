import { get } from 'svelte/store';
import { isPaneDrawer, paneOpen } from '$lib/stores/paneOpen';
import { activeThreadId } from '$lib/stores/threads';
import { focusPlanNode } from '$lib/planNodeFocus';
import { openArtifactTab, openLegacyDiscussionArchiveTab, openSubagentConversationTab } from '$lib/stores/artifactWorkspace';
import type { OperatorArtifactLink } from '$lib/api/plans';
import type { ArtifactTab } from '$lib/artifactTabTypes';

/**
 * Returns to the plan node that opened a subagent artifact leaf, through the
 * same focus path as an attention item (`planNodeFocus`): the plan shows (the
 * pane, or the plan drawer on a phone) with that node revealed and focused. A
 * node of a nested subagent plan carries its owning step, shown when the
 * nested row no longer is.
 */
export async function returnToPlanNode(
	source: Pick<ArtifactTab, 'sourceNodeId' | 'sourceStepId'> | null | undefined,
): Promise<void> {
	const threadId = get(activeThreadId);
	if (source?.sourceNodeId && threadId) {
		await focusPlanNode(threadId, source.sourceNodeId, source.sourceStepId ?? null);
	}
}

/**
 * Opens a plan child's subagent conversation as an artifact tab. A docked pane
 * stays as it is; an overlay pane (`isPaneDrawer`) closes to show the tab, as
 * every other artifact opener does.
 */
export function openPlanSubagentLeaf(
	threadId: string | null,
	nodeId: string,
	childThreadId: string,
	title: string,
	stepId?: string,
): void {
	if (!threadId) return;
	openSubagentConversationTab(threadId, {
		threadId: childThreadId, title, sourceNodeId: nodeId, sourceStepId: stepId,
	});
	if (isPaneDrawer()) paneOpen.set(false);
}

/** Opens a plan ask in its one canonical surface. */
export function openPlanAsk(
	threadId: string | null,
	artifact: OperatorArtifactLink,
	nodeTitle: string,
	openDecision: () => void,
): void {
	if (!threadId) return;
	if (artifact.type === 'plan-decision') {
		openDecision();
		return;
	}
	openArtifactTab(threadId, {
		artifactId: artifact.id,
		path: artifact.path ?? artifact.external_id,
		title: artifact.title ?? nodeTitle,
	});
	if (isPaneDrawer()) paneOpen.set(false);
}

/** Opens a plan child's legacy discussion archive as an artifact tab. */
export function openPlanDiscussionLeaf(
	threadId: string | null,
	artifactId: string,
	title: string,
): void {
	if (!threadId) return;
	openLegacyDiscussionArchiveTab(threadId, {
		artifactId,
		title: `Discussion archive — ${title}`,
	});
	if (isPaneDrawer()) paneOpen.set(false);
}
