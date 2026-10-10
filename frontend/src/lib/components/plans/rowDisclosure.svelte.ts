/**
 * Which of a plan tree's rows the user has opened: branches (their children
 * shown) and details panels. A branch the user never touched follows
 * `openByDefault`; an explicit collapse or expand wins over it.
 */
export function createRowDisclosure(openByDefault: (nodeId: string) => boolean) {
	let expandedBranchIds = $state<string[]>([]);
	let collapsedBranchIds = $state<string[]>([]);
	let expandedDetailIds = $state<string[]>([]);

	function isBranchExpanded(nodeId: string): boolean {
		if (collapsedBranchIds.includes(nodeId)) return false;
		if (expandedBranchIds.includes(nodeId)) return true;
		return openByDefault(nodeId);
	}

	function toggleBranch(nodeId: string): void {
		if (isBranchExpanded(nodeId)) {
			collapsedBranchIds = [...collapsedBranchIds.filter((id) => id !== nodeId), nodeId];
			expandedBranchIds = expandedBranchIds.filter((id) => id !== nodeId);
			return;
		}
		expandedBranchIds = [...expandedBranchIds.filter((id) => id !== nodeId), nodeId];
		collapsedBranchIds = collapsedBranchIds.filter((id) => id !== nodeId);
	}

	function openBranches(nodeIds: string[]): void {
		expandedBranchIds = [...new Set([...expandedBranchIds, ...nodeIds])];
		collapsedBranchIds = collapsedBranchIds.filter((id) => !nodeIds.includes(id));
	}

	function detailsExpanded(nodeId: string): boolean {
		return expandedDetailIds.includes(nodeId);
	}

	function toggleDetails(nodeId: string): void {
		expandedDetailIds = detailsExpanded(nodeId)
			? expandedDetailIds.filter((id) => id !== nodeId)
			: [...expandedDetailIds, nodeId];
	}

	return { isBranchExpanded, toggleBranch, openBranches, detailsExpanded, toggleDetails };
}
