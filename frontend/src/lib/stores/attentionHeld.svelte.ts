/**
 * Approving or declining a held reply, owned outside the drawer because the drawer closes the
 * moment the dock advances. Same shape as `submitAnswer`: the item leaves the queue at once with
 * its own receipt line, and a failed write returns it, reopened, with the reason.
 */
import { get } from "svelte/store";
import type { HeldReplyDetail } from "$lib/api/attentionQueue";
import { decideHeldReply, type HeldVerb } from "$lib/api/heldReplies";
import { token } from "$lib/stores/auth";
import { activeId, expandedId, identityEpoch, queue, reload, visibleQueue } from "./attentionDock.svelte";
import { rollBack } from "./attentionAnswer.svelte";
import { answers, setAnswer, showNotice } from "./attentionAnswerState";

export const declinedLine = (recipient: string) => `The reply to ${recipient} was not sent.`;
const approvedLine = (recipient: string) => `The reply to ${recipient} is approved and not yet sent.`;
export const failedLine = (recipient: string) =>
  `Couldn’t send the reply to ${recipient} because of an unexpected error. Approve once to try again.`;

export async function decideHeld(held: HeldReplyDetail, verb: HeldVerb, onStarted?: () => void): Promise<void> {
  const id = held.id;
  if (get(answers)[id]?.state === "sending") return;
  const bearer = get(token);
  if (!bearer) {
    rollBack(id, "unauthorized", "This reply isn’t available — sign in and try again.");
    return;
  }
  const generation = identityEpoch();
  const items = get(visibleQueue).items;
  const at = items.findIndex((item) => item.external_id === id);
  const follower = at >= 0 ? items[at + 1] : undefined;
  showNotice(null);
  // Let the dock retain this drawer before the immediate queue update removes its item.
  onStarted?.();
  setAnswer(id, {
    state: "sending",
    line: verb === "approve" ? approvedLine(held.recipient) : declinedLine(held.recipient),
  });
  expandedId.set(null);
  activeId.set(follower ? follower.external_id : null);

  const outcome = await decideHeldReply(bearer, id, verb);
  if (generation !== identityEpoch()) return;
  if (outcome.status === "unauthorized") {
    rollBack(id, "unauthorized", "This reply isn’t available — sign in and try again.");
    return;
  }
  if (outcome.status === "error") {
    // The server keeps the reply held, with its own line when it has one; Approve once tries again.
    rollBack(id, "error", outcome.line ?? failedLine(held.recipient), outcome.ref);
    await reload();
    return;
  }
  queue.update((q) => {
    const rest = q.items.filter((item) => item.external_id !== id);
    return { current: rest[0] ?? null, count: Math.max(0, q.count - (rest.length < q.items.length ? 1 : 0)), items: rest };
  });
  setAnswer(id, null);
  // The sender's receipt lands in its chat; until then the dock says what happened to the reply.
  if (outcome.status === "declined") showNotice(declinedLine(held.recipient));
  else if (outcome.status === "not_sent") showNotice(outcome.line);
  await reload();
}
