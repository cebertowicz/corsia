/**
 * Sending a decision's answer from Activity. It writes no attention state: Activity reports the outcome itself.
 */
import {
  answerDecision,
  isPickAny,
  recommendedIds,
  type AnswerOutcome,
  type DecisionDetail,
} from "$lib/api/attentionQueue";

export function newMutationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `decision-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Sends a draft as the decision's answer and returns what the server said. Any surface can report the outcome its own way.
 */
export function sendAnswer(
  decision: DecisionDetail,
  draft: { selected: string | null; picked: string[] | null; other: string },
  mutationId: string,
): Promise<AnswerOutcome> {
  return answerDecision(
    decision.id,
    isPickAny(decision)
      ? {
          optionIds: draft.picked ?? recommendedIds(decision),
          otherText: draft.other.trim(),
          expectedRevision: decision.revision,
        }
      : { optionId: draft.selected ?? undefined, otherText: draft.other.trim(), expectedRevision: decision.revision },
    mutationId,
  );
}
