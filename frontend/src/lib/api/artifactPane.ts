/**
 * The artifact pane DTO contract: `GET /api/threads/{id}/artifacts`.
 *
 * Split out of `docs.ts` so that module stays inside its code-health budget.
 * The pane's HTTP calls stay in `docs.ts` alongside the register/hide/reconcile
 * surface they share request helpers with; this module is the shape those calls
 * and the pane components agree on. Mirrors `backend/registry/view.py`.
 */

export type ArtifactCapability = 'leaf-package' | 'external-link' | 'download' | 'unsupported';

export interface ArtifactAction {
	capability: ArtifactCapability;
	disabled: boolean;
	disabled_reason: string | null;
}
export interface ArtifactPresentation {
	label: string;
	icon: string;
	capability: ArtifactCapability;
}
export interface ArtifactNode {
	kind: 'artifact';
	id: string;
	integration: string;
	type: string;
	external_id: string;
	title: string | null;
	name: string;
	path: string | null;
	retention: 'ephemeral' | 'durable';
	surfacing: 'visible' | 'quiet';
	/** Owner-pinned: kept in the pane after the originating plan task settles. */
	pinned: boolean;
	/** False for a subscriber-only row: pinning is an owner-registration override. */
	pin_eligible: boolean;
	/** Shared document, user-commented document or open pull request: never task-retired, pin affects order only. */
	retirement_exempt: boolean;
	state: string;
	updated_at: string | null;
	description: string | null;
	/** A non-user reply needs the user's attention when the document opens. */
	reply_waiting?: boolean;
	metadata: Record<string, unknown>;
	presentation: ArtifactPresentation;
	action: ArtifactAction;
}
/** Derived presentation group (e.g. a live Git worktree) — never a stored artifact. */
export interface WorktreeGroupNode {
	kind: 'worktree-group';
	key: string;
	label: string;
	branch: string;
	file_count: number;
	/** True when any child is in ``needs_attention``. */
	attention: boolean;
	children: ArtifactNode[];
}
export type PaneNode = ArtifactNode | WorktreeGroupNode;
export interface NeedsAttentionItem {
	kind: 'repair-failure';
	artifact_id: string;
	reason: string;
	retryable: boolean;
}
export interface PaneData {
	nodes: PaneNode[];
	needs_attention: NeedsAttentionItem[];
}
