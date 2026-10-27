import { openPlanSubagentLeaf, openPlanDiscussionLeaf } from '$lib/planLeafReturn';

export function createPlanDestinations(activeThreadId: () => string | null) {
	return {
		openPlanSubagent(nodeId: string, childThreadId: string, title: string, stepId?: string): void {
			openPlanSubagentLeaf(activeThreadId(), nodeId, childThreadId, title, stepId);
		},
		openPlanDiscussion(artifactId: string, title: string): void {
			openPlanDiscussionLeaf(activeThreadId(), artifactId, title);
		},
	};
}
