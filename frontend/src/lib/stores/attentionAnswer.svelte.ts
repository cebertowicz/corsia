/**
 * Sending a decision's answer, owned outside the chip because the chip
 * unmounts the moment the dock advances. The dock state it reads and writes
 * lives in `./attentionDock.svelte`.
 */
import { get } from "svelte/store";
import {
  answerDecision,
  isPickAny,
  OTHER_OPTION_ID,
  recommendedIds,
  type DecisionDetail,
} from "$lib/api/attentionQueue";
import { token } from "$lib/stores/auth";
import {
  activeId,
  clearDraft,
  draftFor,
  expandedId,
  identityEpoch,
  queue,
  reload,
  visibleQueue,
} from "./attentionDock.svelte";
import { answers, setAnswer, showNotice, type AnswerState } from "./attentionAnswerState";

function newMutationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `decision-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Sends the decision's draft as its answer. The dock treats the answer as
 * recorded the moment it is sent: the chip closes and the next item takes its
 * place, while the write finishes behind it. A refused or failed write rolls
 * back: the decision returns with its draft and retry key intact and the
 * reason on it. This lives here rather than in the chip because the chip
 * unmounts when the dock advances.
 */
export async function submitAnswer(decision: DecisionDetail): Promise<void> {
  const decisionId = decision.id;
  const draft = draftFor(decisionId);
  const pickAny = isPickAny(decision);
  const optionId = draft.selected;
  if (get(answers)[decisionId]?.state === "sending") return;
  // Pick any has no "nothing chosen" guard: none is a valid answer.
  if (!pickAny) {
    if (!optionId) return;
    if (optionId === OTHER_OPTION_ID && !draft.other.trim()) return;
  }
  const bearer = get(token);
  if (!bearer) {
    rollBack(decisionId, "unauthorized", "This decision isn’t available — sign in and try again.");
    return;
  }
  if (!draft.mutationId) draft.mutationId = newMutationId();
  const generation = identityEpoch();

  // Advance to what followed the answered item, not back to the head.
  const items = get(visibleQueue).items;
  const at = items.findIndex((item) => item.external_id === decisionId);
  const follower = at >= 0 ? items[at + 1] : undefined;
  showNotice(null);
  setAnswer(decisionId, { state: "sending" });
  expandedId.set(null);
  activeId.set(follower ? follower.external_id : null);

  const outcome = await answerDecision(
    bearer,
    decisionId,
    pickAny
      ? {
          optionIds: draft.picked ?? recommendedIds(decision),
          otherText: draft.other.trim(),
          expectedRevision: decision.revision,
        }
      : { optionId: optionId ?? undefined, otherText: draft.other.trim(), expectedRevision: decision.revision },
    draft.mutationId,
  );
  if (generation !== identityEpoch()) return;
  if (outcome.status === "ok" || outcome.status === "gone") {
    draft.mutationId = null;
    clearDraft(decisionId);
    // Drop the item locally first, so a slow or failed re-read cannot show it again.
    queue.update((q) => {
      const rest = q.items.filter((item) => item.external_id !== decisionId);
      return {
        current: rest[0] ?? null,
        count: Math.max(0, q.count - (rest.length < q.items.length ? 1 : 0)),
        items: rest,
      };
    });
    setAnswer(decisionId, null);
    // A question that left the queue first is not an error to retry: say so
    // once, and the dock has already moved on to the next live item.
    if (outcome.status === "gone") showNotice(outcome.message);
    await reload();
    return;
  }
  if (outcome.status === "unauthorized") {
    rollBack(decisionId, "unauthorized", "This decision isn’t available.");
  } else if (outcome.status === "stale") {
    rollBack(decisionId, "stale", "This decision changed — reloading it.");
    await reload();
  } else if (outcome.status === "rejected") {
    // Fixable by this user right now; the draft is preserved.
    rollBack(decisionId, "rejected", outcome.message);
  } else {
    rollBack(decisionId, "error", "That answer couldn’t be saved. Try again.");
  }
}

/**
 * Returns an answer that was shown as recorded. The decision comes back at the
 * front and reopens so the error and Retry are visible immediately.
 */
function rollBack(
  decisionId: string,
  kind: Extract<AnswerState, { state: "failed" }>["kind"],
  message: string,
): void {
  setAnswer(decisionId, { state: "failed", kind, message });
  activeId.set(decisionId);
  expandedId.set(decisionId);
}
