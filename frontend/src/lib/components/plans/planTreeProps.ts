import type { OperatorArtifactLink, OperatorNode, UserActionHandler } from '$lib/api/plans';
import type { PaneView } from './foldMotion.svelte';
import type { FoldedWork } from './planFoldLevels';
import type { ChildRowState } from './childExpansion.svelte';

/** Inputs and actions for a plan tree, including nested subagent plans. */
export interface PlanTreeProps {
 nodes: OperatorNode[];
 /** Scopes DOM ids, flip keys and fold levels when a child plan nests in this tree. */
 planId?: string;
 /** The depth this tree's roots render at: a nested child plan's parent row + 1. */
 depthOffset?: number;
 currentNodeIds?: string[];
 nextNodeIds?: string[];
 maxDepth?: number;
 /** Drill-down owns a deliberately expanded focused subtree; overview does not. */
 expandBranchesByDefault?: boolean;
 isChildExpanded?: (nodeId: string) => boolean;
 childRowState?: (nodeId: string) => ChildRowState;
 onToggleChild?: (nodeId: string) => void;
 onRetryChild?: (nodeId: string) => void;
 onDrill?: (nodeId: string) => void;
 onOpenChild?: (nodeId: string, threadId: string, title: string, stepId?: string) => void;
 onOpenArtifact?: (artifact: OperatorArtifactLink, nodeTitle: string) => void;
 onOpenLegacyDiscussion?: (artifactId: string, title: string) => void;
 userActionBusy?: boolean;
 onUserAction?: UserActionHandler;
 onRefresh?: () => void;
 /** What the user has seen of this plan in the pane; only the pane's own tree passes it. */
 paneView?: PaneView | null;
 /** The server's folds and where older finished rows are read from. */
 folded?: FoldedWork | null;
}
