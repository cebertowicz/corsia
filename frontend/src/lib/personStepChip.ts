import type { OperatorArtifactLink, OperatorEnvelope, OperatorNode } from '$lib/api/plans';
import type { UpdateCard } from '$lib/api/threadUpdates';
import { personStepInstructionsDocument } from '$lib/components/plans/nodeDetailsText';
import { focusPlanNode } from '$lib/planNodeFocus';
import { openPlanAsk } from '$lib/planLeafReturn';
import { peekEntry, refresh } from '$lib/stores/plans.svelte';
import { planKeyString } from '$lib/stores/planKeys';

/** How a person step stands: the plan owns it; the card's own word only covers a step the plan read no longer lists. */
export type StepState = 'open' | 'done' | 'closed';

export function stepState(card: Pick<UpdateCard, 'step_state'>, node: Pick<OperatorNode, 'state'> | undefined): StepState {
	if (node) return node.state === 'accepted' || node.state === 'accepted_with_followup' ? 'done' : node.state === 'closed' ? 'closed' : 'open';
	return card.step_state ?? 'open';
}

/** What Do → opens: the step's instructions document when it has one, else the step's details in the plan. */
export type DoTarget =
	| { kind: 'document'; artifact: OperatorArtifactLink }
	| { kind: 'details' };

export function doTarget(node: OperatorNode | undefined): DoTarget {
	const artifact = node ? personStepInstructionsDocument(node) : undefined;
	return artifact ? { kind: 'document', artifact } : { kind: 'details' };
}

/** The one line a group of ready steps reads as: "7 steps". */
export function groupLabel(count: number): string {
	return `${count} steps`;
}

function threadNode(threadId: string, nodeId: string): OperatorNode | undefined {
	const payload = peekEntry(planKeyString({ kind: 'thread', id: threadId })).payload as OperatorEnvelope | null;
	return payload?.plans.flatMap((plan) => plan.nodes).find((node) => node.id === nodeId);
}

/**
 * Do → on a step: the same two opens the plan row makes (`ThreadPlanPane.focusNode`), never a second
 * path. The plan is read first when the chip's own read has not landed, so a press never opens stale.
 */
export async function doStep(card: Pick<UpdateCard, 'thread_id' | 'node_id' | 'title'>): Promise<void> {
	if (!card.node_id) return;
	let node = threadNode(card.thread_id, card.node_id);
	if (!node) {
		await refresh({ kind: 'thread', id: card.thread_id });
		node = threadNode(card.thread_id, card.node_id);
	}
	const target = doTarget(node);
	if (target.kind === 'document') {
		openPlanAsk(card.thread_id, target.artifact, card.title ?? node?.title ?? 'Step', () => {});
		return;
	}
	await focusPlanNode(card.thread_id, card.node_id, null, true);
}

/** Do → on a group: the plan, at the first step still waiting, where each step opens by its own row. */
export async function doFirstOf(cards: Array<Pick<UpdateCard, 'thread_id' | 'node_id'>>): Promise<void> {
	const first = cards.find((card) => card.node_id);
	if (first?.node_id) await focusPlanNode(first.thread_id, first.node_id);
}
