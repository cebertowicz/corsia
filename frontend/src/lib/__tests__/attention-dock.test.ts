/**
 * The dock and its chip, in isolation: the interactions the composed host test
 * cannot drive cheaply.
 *
 * Red at `19ea844a`: none of these modules exist, and the retired
 * `PlanDecisionDock` sent an unrelated Other draft with a listed option — an
 * unrecoverable dead end reported to the user as staleness.
 */
import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AttentionDock from "$lib/components/attention/AttentionDock.svelte";
import { __reset } from "$lib/stores/attentionDock.svelte";
import { token } from "$lib/stores/auth";
import { activeThreadId } from "$lib/stores/threads";
import { connectionState } from "$lib/stores/connection";
import type { AttentionQueue } from "$lib/api/attentionQueue";
import {
  decisionItem,
  externalReviewItem,
  queueBody,
} from "./fixtures/attentionQueue";

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;
let calls: Array<{ url: string; init?: RequestInit }>;
let answerStatus: (attempt: number) => { status: number; payload?: unknown };

function respond(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
    headers: { get: () => null },
  };
}

function stubNetwork() {
  let attempt = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      if (String(url).includes("/api/attention/queue"))
        return respond(200, body);
      attempt += 1;
      const outcome = answerStatus(attempt);
      return respond(
        outcome.status,
        outcome.payload ?? { decision: { state: "answered" } },
      );
    }),
  );
}

async function settle() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
    await tick();
    flushSync();
  }
}

function dockBar(): HTMLButtonElement {
  const bar = host.querySelector(".dock-bar") as HTMLButtonElement;
  expect(bar).toBeTruthy();
  return bar;
}

function optionButton(label: string): HTMLButtonElement {
  const match = Array.from(host.querySelectorAll("button")).find((item) =>
    item.textContent?.includes(label),
  );
  expect(match, label).toBeTruthy();
  return match as HTMLButtonElement;
}

function answerButton(label = "answer"): HTMLButtonElement {
  const match = Array.from(host.querySelectorAll("button")).find(
    (item) => (item.textContent ?? "").trim().toLowerCase() === label,
  );
  expect(match).toBeTruthy();
  return match as HTMLButtonElement;
}

function otherInput(): HTMLTextAreaElement {
  const el = host.querySelector(".other-option textarea") as HTMLTextAreaElement;
  expect(el).toBeTruthy();
  return el;
}

function typeOther(text: string) {
  const el = otherInput();
  el.value = text;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function answerBodies(): Array<Record<string, unknown>> {
  return calls
    .filter((call) => call.url.includes("/answer"))
    .map((call) => JSON.parse(call.init!.body as string));
}

async function openChip(props: Record<string, unknown> = {}) {
  component = mount(AttentionDock, { target: host, props });
  await settle();
  dockBar().click();
  await settle();
}

beforeEach(() => {
  // The drawer portals to document.body, so the host is the body itself.
  host = document.body;
  calls = [];
  body = queueBody(decisionItem(), 3);
  answerStatus = () => ({ status: 200 });
  token.set("test-bearer");
  connectionState.set("connected");
  __reset();
  vi.unstubAllGlobals();
  stubNetwork();
});
afterEach(() => {
  if (component) unmount(component);
  component = null;
  host.replaceChildren();
  __reset();
  connectionState.set("disconnected");
  activeThreadId.set(null);
  vi.restoreAllMocks();
});

describe("AttentionDock — reply attention refresh", () => {
  it("refreshes reply counts on resolution and reopening, but ignores unrelated frames", async () => {
    const { emit } = await import("$lib/ws/events");
    const reply = {
      detail: { title: "brief.md", unread_count: 1, newest_at: "2026-10-27",
        destination: { pane: "document-replies", id: "doc" }, source: { thread_id: "thread" } },
    };
    activeThreadId.set("thread");
    body = { ...queueBody(null, 0), reply_items: [reply] };
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(host.querySelector('.reply-chip-notice')?.textContent).toContain('1 new reply');
    calls.length = 0;
    body = { ...queueBody(null, 0), reply_items: [] };
    emit("doc_comment_changed", { type: "doc_comment_changed", artifact_id: "doc" });
    await settle();
    expect(calls.filter((call) => call.url.includes("/api/attention/queue"))).toHaveLength(0);
    expect(host.querySelector('.reply-chip-notice')).not.toBeNull();
    emit("doc_comment_changed", { type: "doc_comment_changed", artifact_id: "doc", reply_attention_changed: true });
    await settle();
    expect(host.querySelector('.reply-chip-notice')).toBeNull();
    body = { ...queueBody(null, 0), reply_items: [reply] };
    emit("doc_comment_changed", { type: "doc_comment_changed", artifact_id: "doc", reply_attention_changed: true });
    await settle();
    expect(host.querySelector('.reply-chip-notice')?.textContent).toContain('1 new reply');
    activeThreadId.set(null);
  });
});

describe("AttentionDock — one item at a time", () => {
  it("shows a one-line bar with the head item, its source, and the waiting count", async () => {
    component = mount(AttentionDock, { target: host });
    await settle();
    const bar = dockBar();
    expect(bar.textContent).toContain("Where should decision state live?");
    expect(bar.textContent).toContain("Deep subagent");
    expect(bar.textContent).toContain("1 of 3");
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
  });

  it("renders nothing at all when nothing waits", async () => {
    body = queueBody(null, 0);
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(host.querySelector("[data-attention-dock]")).toBeFalsy();
  });

  it("omits the count when only one item waits", async () => {
    body = queueBody(decisionItem(), 1);
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(dockBar().textContent).not.toMatch(/\d of \d/);
  });

  it("opens the full chip only on an explicit click", async () => {
    await openChip();
    expect(host.querySelectorAll(".decision-chip").length).toBe(1);
    expect(
      host.querySelector('[data-adaptive-drawer] [role="dialog"]'),
    ).toBeTruthy();
    // The drawer chrome supplies the dismiss control; the decision's own header
    // keeps the title, so the chrome carries no duplicate heading.
    expect(host.querySelector(".drawer-done")).toBeTruthy();
    expect(host.querySelector(".drawer-head h2")).toBeNull();
    expect(host.textContent).toContain("P5 needs one canonical home");
  });

  it("renders exactly one decision eyebrow beside Done at 393 and 1440", async () => {
    for (const width of [393, 1440]) {
      component = mount(AttentionDock, { target: host });
      await settle();
      dockBar().click();
      await settle();

      const drawer = host.querySelector("[data-adaptive-drawer]") as HTMLElement;
      expect(drawer.querySelectorAll(".drawer-context"), `${width}px chrome context line`).toHaveLength(1);
      expect(drawer.querySelectorAll(".decision-chip .eyebrow"), `${width}px duplicate body eyebrow`).toHaveLength(0);

      unmount(component);
      component = null;
      host.replaceChildren();
      __reset();
    }
  });

  it("closes back to the bar and returns focus to it", async () => {
    await openChip();
    (host.querySelector(".drawer-done") as HTMLElement).click();
    await settle();
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(document.activeElement).toBe(dockBar());
  });

  it("collapses an open chip when the head moves under the user", async () => {
    await openChip();
    expect(host.querySelectorAll(".decision-chip").length).toBe(1);

    body = queueBody(
      decisionItem({ id: "decision-2", title: "A different question" }),
      2,
    );
    const { reload } = await import("$lib/stores/attentionDock.svelte");
    await reload();
    await settle();

    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(dockBar().textContent).toContain("A different question");
  });

  it("empties cleanly when the last item is invalidated elsewhere", async () => {
    await openChip();
    body = queueBody(null, 0);
    const { reload } = await import("$lib/stores/attentionDock.svelte");
    await reload();
    await settle();
    expect(host.querySelector("[data-attention-dock]")).toBeFalsy();
  });
});

describe("DecisionChip — answering", () => {
  it("sends the selected option, expected revision, and a durable retry key", async () => {
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(
      calls.some((c) =>
        c.url.includes("/api/plans/decisions/decision-1/answer"),
      ),
    ).toBe(true);
    expect(sent.option_id).toBe("A");
    expect(sent.other_text).toBeNull();
    expect(sent.expected_revision).toBe(3);
    expect(typeof sent.mutation_id).toBe("string");
  });

  it("choosing a listed option after typing in Other clears the draft and succeeds", async () => {
    // The exact head defect: this combination was an unrecoverable dead end.
    await openChip();
    typeOther("Actually, split this into two.");
    await settle();
    optionButton("Ship the twin").click();
    await settle();

    expect(otherInput().value).toBe("");
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(sent.option_id).toBe("A");
    expect(sent.other_text).toBeNull();
  });

  it("sends Other text only under the reserved other option id", async () => {
    await openChip();
    typeOther("  Do neither; split it.  ");
    await settle();
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(sent.option_id).toBe("other");
    expect(sent.other_text).toBe("Do neither; split it.");
  });

  it("shows the answer as recorded and advances before the write returns", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init });
        if (String(url).includes("/api/attention/queue"))
          return respond(200, body);
        await gate;
        return respond(200, { decision: { state: "answered" } });
      }),
    );
    const a = decisionItem({ id: "decision-a", title: "Question A" });
    const b = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(a, 2, [a, b]);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    // The write is still in flight: the chip is gone, the receipt shows, and
    // the next decision has taken its place with the count already down.
    expect(answerBodies()).toHaveLength(1);
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(host.textContent).toContain("Answer recorded");
    expect(dockBar().textContent).toContain("Question B");
    expect(host.textContent).not.toContain("of 2");

    body = queueBody(b, 1, [b]);
    release!();
    await settle();
    expect(answerBodies()).toHaveLength(1);
    expect(host.textContent).not.toContain("Answer recorded");
    expect(dockBar().textContent).toContain("Question B");
  });

  it("leaves nothing but the receipt when the last decision is answered", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init });
        if (String(url).includes("/api/attention/queue"))
          return respond(200, body);
        await gate;
        return respond(200, { decision: { state: "answered" } });
      }),
    );
    body = queueBody(decisionItem(), 1);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    expect(host.querySelector(".dock-bar")).toBeNull();
    expect(host.textContent).toContain("Answer recorded");

    body = queueBody(null, 0);
    release!();
    await settle();
    expect(host.textContent).not.toContain("Answer recorded");
    expect(host.querySelector("[data-attention-dock]")).toBeFalsy();
  });

  it("never submits without a chosen option or non-empty Other text", async () => {
    await openChip();
    expect(answerButton().disabled).toBe(true);
    typeOther("   ");
    await settle();
    expect(answerButton().disabled).toBe(true);
  });
});

it("keyboard focus alone never discards the chosen option", async () => {
  // Traversal passes through this input on every pass. Choosing Other there
  // silently unpresses the option, disables Answer, and drops the user out
  // of the chip with nothing announced.
  await openChip();
  optionButton("Ship the twin").click();
  await settle();

  otherInput().dispatchEvent(new FocusEvent("focus", { bubbles: false }));
  await settle();

  expect(optionButton("Ship the twin").getAttribute("aria-pressed")).toBe(
    "true",
  );
  expect(answerButton().disabled).toBe(false);
});

it("pointing at the Other field does choose it", async () => {
  await openChip();
  optionButton("Ship the twin").click();
  await settle();

  otherInput().dispatchEvent(new Event("pointerdown", { bubbles: true }));
  await settle();

  expect(optionButton("Ship the twin").getAttribute("aria-pressed")).toBe(
    "false",
  );
});

describe("DecisionChip — truthful failure copy", () => {
  it("reports a 422 as a fixable refusal and keeps the draft", async () => {
    answerStatus = () => ({
      status: 422,
      payload: { detail: "free text belongs to the Other option" },
    });
    await openChip();
    typeOther("Some text");
    await settle();
    answerButton().click();
    await settle();

    expect(host.textContent).toContain("free text belongs to the Other option");
    expect(host.textContent).not.toMatch(/this decision changed/i);
    expect(otherInput().value).toBe("Some text");
    expect(host.querySelectorAll(".decision-chip").length).toBe(1);
  });

  it("reports a 409 as real movement and re-reads canonical truth", async () => {
    answerStatus = () => ({
      status: 409,
      payload: { detail: "plan decision revision conflict" },
    });
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    expect(host.textContent).toMatch(/changed/i);
    expect(
      calls.filter((c) => c.url.includes("/api/attention/queue")).length,
    ).toBeGreaterThan(1);
  });

  it("keeps one mutation id across a lost response and its retry", async () => {
    answerStatus = (attempt) =>
      attempt === 1 ? { status: 500 } : { status: 200 };
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();
    expect(host.textContent).toMatch(/couldn’t|could not/i);

    answerButton("retry").click();
    await settle();

    const sent = answerBodies();
    expect(sent).toHaveLength(2);
    expect(sent[1].mutation_id).toBe(sent[0].mutation_id);
    expect(host.textContent).not.toMatch(/couldn’t|could not/i);
  });

  it("rolls a failed write back: the decision returns with its draft, reason and Retry", async () => {
    answerStatus = () => ({ status: 500 });
    await openChip();
    typeOther("Do neither; split it.");
    await settle();
    answerButton().click();
    await settle();

    // Back where it was, open, with the reason and the user's own words.
    expect(host.querySelectorAll(".decision-chip").length).toBe(1);
    expect(host.textContent).toContain("That answer couldn’t be saved");
    expect(otherInput().value).toBe("Do neither; split it.");
    expect(answerButton("retry").disabled).toBe(false);
    expect(host.textContent).not.toContain("Answer recorded");
  });

  it("reopens a failed earlier write in front of another decision", async () => {
    answerStatus = () => ({ status: 500 });
    const a = decisionItem({ id: "decision-a", title: "Question A" });
    const b = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(a, 2, [a, b]);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    // Send A, then open B before A's failure lands.
    let reject: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      reject = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init });
        if (String(url).includes("/api/attention/queue"))
          return respond(200, body);
        await gate;
        return respond(500, {});
      }),
    );
    answerButton().click();
    await settle();
    dockBar().click();
    await settle();
    expect(host.textContent).toContain("Question B");

    reject!();
    await settle();

    expect(host.textContent).toContain("Question A");
    expect(host.textContent).toContain("That answer couldn’t be saved");
    const { visibleQueue } = await import("$lib/stores/attentionDock.svelte");
    const { answers } = await import("$lib/stores/attentionAnswerState");
    const { get } = await import("svelte/store");
    expect(get(visibleQueue).items.map((i) => i.external_id)).toEqual([
      "decision-a",
      "decision-b",
    ]);
    expect(get(answers)["decision-a"]).toMatchObject({ state: "failed", kind: "error" });
  });

  it("explains an unauthorized decision and stops offering the write", async () => {
    answerStatus = () => ({ status: 403 });
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    expect(host.textContent).toMatch(/isn’t available|not available/i);
    expect(answerButton().disabled).toBe(true);
  });

  it("never leaks a raw server body or internal identifier", async () => {
    answerStatus = () => ({
      status: 500,
      payload: { detail: "unknown plan: internal-secret" },
    });
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    expect(host.textContent).not.toContain("internal-secret");
    expect(host.textContent).not.toContain("plan-1");
    expect(host.textContent).not.toContain("node-decision");
  });

  it("holds writes and explains why while the surface is stale", async () => {
    await openChip({ writesDisabled: true });
    optionButton("Ship the twin").click();
    await settle();
    expect(answerButton().disabled).toBe(true);
    expect(host.textContent).toMatch(/paused/i);
  });
});

describe("DecisionChip — attribution and accessibility", () => {
  it("names the recommended option by label, never a bare id", async () => {
    await openChip();
    expect(optionButton("Reuse the identity").textContent).toContain("Recommended");
  });

  it("labels a root-direct raise truthfully rather than implying a review", async () => {
    body = queueBody(
      decisionItem({
        source: {
          ...decisionItem().detail.source,
          parent_reviewed: false,
        } as never,
      }),
      1,
    );
    await openChip();
    expect(host.textContent).toMatch(/raised directly — no parent review/i);
  });

  it("does not claim a review for delegated work that had one", async () => {
    await openChip();
    expect(host.textContent).not.toMatch(/raised directly/i);
  });

  it("exposes options as pressable controls with truthful pressed state", async () => {
    await openChip();
    expect(optionButton("Ship the twin").getAttribute("aria-pressed")).toBe(
      "false",
    );
    optionButton("Ship the twin").click();
    await settle();
    expect(optionButton("Ship the twin").getAttribute("aria-pressed")).toBe(
      "true",
    );
    optionButton("Reuse the identity").click();
    await settle();
    expect(optionButton("Ship the twin").getAttribute("aria-pressed")).toBe(
      "false",
    );
  });

  it("exposes the collapsed bar state through aria-expanded", async () => {
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(dockBar().getAttribute("aria-expanded")).toBe("false");
  });

  it("moves focus into the chip when the bar expands into it", async () => {
    // The trigger unmounts on open, so leaving focus behind drops a keyboard
    // user to the document body in the middle of the task.
    await openChip();
    expect(document.activeElement).toBe(
      host.querySelector(".decision-chip h3"),
    );
  });

  it("collapses on Escape and returns focus to the bar", async () => {
    await openChip();
    host
      .querySelector(".decision-chip")!
      .dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    await settle();
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(document.activeElement).toBe(dockBar());
  });

  it("names its region for assistive technology", async () => {
    await openChip();
    const region = host.querySelector("section[aria-labelledby]");
    expect(region).toBeTruthy();
    expect(
      document.getElementById(region!.getAttribute("aria-labelledby")!)
        ?.textContent,
    ).toContain("Where should decision state live?");
  });
});

describe("AttentionDock — a plan indicator focuses this one dock", () => {
  it("opens the canonical chip when another surface requests focus", async () => {
    const { requestFocus } = await import("$lib/stores/attentionDock.svelte");
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);

    requestFocus();
    await settle();

    expect(host.querySelectorAll(".decision-chip").length).toBe(1);
  });

  it("honours a second request after the user closed the chip", async () => {
    const { requestFocus } = await import("$lib/stores/attentionDock.svelte");
    component = mount(AttentionDock, { target: host });
    await settle();
    requestFocus();
    await settle();
    (host.querySelector(".drawer-done") as HTMLElement).click();
    await settle();
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);

    requestFocus();
    await settle();

    expect(host.querySelectorAll(".decision-chip").length).toBe(1);
  });

  it("a focus request while suppressed does not open anything", async () => {
    const { requestFocus } = await import("$lib/stores/attentionDock.svelte");
    component = mount(AttentionDock, {
      target: host,
      props: { suppressed: true },
    });
    await settle();
    requestFocus();
    await settle();
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(host.querySelector(".dock-bar")?.getAttribute("aria-disabled")).toBe(
      "true",
    );
  });
});

describe("ExternalReviewRow — hand off, never resolve", () => {
  it("offers exactly one action: open the owning renderer", async () => {
    body = queueBody(externalReviewItem(), 1);
    const opened: unknown[] = [];
    await openChip({ onOpenDestination: (d: unknown) => opened.push(d) });

    expect(host.textContent).toContain("Reviewer findings on PR 9");
    expect(host.textContent).toContain("2 of 5 left");
    (
      Array.from(host.querySelectorAll("button")).find((b) =>
        b.textContent?.includes("Open in documents"),
      ) as HTMLButtonElement
    ).click();
    await settle();

    expect(opened).toEqual([{ version: 1, pane: "documents", id: "doc-9" }]);
  });

  it("names who asked for the review (PRD-PLANS H4)", async () => {
    body = queueBody(externalReviewItem(), 1);
    await openChip();
    expect(host.textContent).toContain("Reviewer subagent");
  });

  it("shows the kind so a lane can never render as a person", async () => {
    body = queueBody(
      externalReviewItem({
        requester: { kind: "service", label: null, lane: "documents" },
      } as never),
      1,
    );
    await openChip();
    expect(host.textContent).toContain("documents");
    expect(host.textContent).toContain("service");
  });

  it("says so, rather than guessing, when no destination was recorded", async () => {
    body = queueBody(externalReviewItem({ destination: null }), 1);
    await openChip();
    expect(host.textContent).toMatch(/no destination recorded/i);
  });

  it("never offers an answer control for someone else’s object", async () => {
    body = queueBody(externalReviewItem(), 1);
    await openChip();
    const labels = Array.from(host.querySelectorAll("button")).map((b) =>
      (b.textContent ?? "").trim().toLowerCase(),
    );
    expect(labels).not.toContain("answer");
  });

  it("offers Skip only in an opened review with another queued item and advances", async () => {
    const review = externalReviewItem({ review_key: "review-a" });
    const next = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(review, 2, [review, next]);
    await openChip();

    const skip = host.querySelector(
      'button[aria-label="Skip to next decision"]',
    ) as HTMLButtonElement;
    expect(skip).toBeTruthy();
    skip.click();
    await settle();
    expect(host.querySelector(".dock-bar")).toBeNull();
    expect(host.textContent).toContain("Question B");
  });

  it("lands focus on the next decision's heading after Skip from a review", async () => {
    const review = externalReviewItem({ review_key: "review-a" });
    const next = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(review, 2, [review, next]);
    await openChip();
    // Opening the review puts focus on its heading, not on the removed opener.
    expect(document.activeElement?.tagName).toBe("H3");
    expect(document.activeElement?.id).toBe("external-review-review-a");

    (
      host.querySelector(
        'button[aria-label="Skip to next decision"]',
      ) as HTMLButtonElement
    ).click();
    await settle();
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement?.tagName).toBe("H3");
    expect(document.activeElement?.textContent).toContain("Question B");
  });

  it("lands focus on the next review's heading after Skip from a decision", async () => {
    const first = decisionItem({ id: "decision-a", title: "Question A" });
    const review = externalReviewItem({ review_key: "review-b" });
    body = queueBody(first, 2, [first, review]);
    await openChip();
    expect(document.activeElement?.textContent).toContain("Question A");

    (
      host.querySelector(
        'button[aria-label="Skip to next decision"]',
      ) as HTMLButtonElement
    ).click();
    await settle();
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement?.tagName).toBe("H3");
    expect(document.activeElement?.id).toBe("external-review-review-b");
  });

  it("hides Skip in an opened review when it is the only item", async () => {
    body = queueBody(externalReviewItem(), 1);
    await openChip();
    expect(
      host.querySelector('button[aria-label="Skip to next decision"]'),
    ).toBeNull();
  });
});

describe("AttentionDock — skip to the next decision", () => {
  const a = decisionItem({ id: "d-a", title: "Question A" });
  const b = decisionItem({ id: "d-b", title: "Question B" });
  const c = decisionItem({ id: "d-c", title: "Question C" });

  function skipButton(): HTMLButtonElement | null {
    return host.querySelector('button[aria-label="Skip to next decision"]');
  }

  it("keeps Skip out of the collapsed row and advances from the opened decision", async () => {
    body = queueBody(a, 3, [a, b, c]);
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(dockBar().textContent).toContain("Question A");
    expect(host.textContent).toContain("1 of 3");
    expect(skipButton()).toBeNull();

    dockBar().click();
    await settle();
    expect(skipButton()?.textContent).toContain("Skip");

    skipButton()!.click();
    await settle();
    expect(host.querySelector(".dock-bar")).toBeNull();
    expect(host.textContent).toContain("Question B");

    skipButton()!.click();
    await settle();
    skipButton()!.click();
    await settle();
    expect(host.textContent).toContain("Question A");
  });

  it("keeps the skipped item waiting and a refresh keeps the user's place", async () => {
    body = queueBody(a, 3, [a, b, c]);
    component = mount(AttentionDock, { target: host });
    await settle();
    dockBar().click();
    await settle();
    skipButton()!.click();
    await settle();
    const { reload } = await import("$lib/stores/attentionDock.svelte");
    await reload();
    await settle();
    expect(host.querySelector(".dock-bar")).toBeNull();
    expect(host.textContent).toContain("Question B");
    expect(host.querySelector("[data-attention-dock]")?.getAttribute("aria-label")).toContain(
      "Question B, 3 waiting",
    );
  });

  it("shows the item after the answered one, not the head", async () => {
    body = queueBody(a, 3, [a, b, c]);
    component = mount(AttentionDock, { target: host });
    await settle();
    dockBar().click();
    await settle();
    skipButton()!.click();
    await settle();
    // The server drops the answered item; the head stays A.
    body = queueBody(a, 2, [a, c]);
    const chipB = Array.from(host.querySelectorAll(".decision-chip")).find((chip) =>
      chip.textContent?.includes("Question B"),
    ) as HTMLElement;
    const shipTwin = Array.from(chipB.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Ship the twin"),
    ) as HTMLButtonElement;
    shipTwin.click();
    await settle();
    const answer = Array.from(chipB.querySelectorAll("button")).find((button) =>
      button.textContent?.trim() === "Answer",
    ) as HTMLButtonElement;
    answer.click();
    await settle();
    expect(dockBar().textContent).toContain("Question C");
    expect(host.textContent).toContain("2 of 2");
  });

  it("keeps the true waiting total above the item cap", async () => {
    body = queueBody(a, 80, [a, b, c]);
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(host.textContent).toContain("1 of 80");
  });

  it("skips inside the open drawer, keeping it open and the draft kept", async () => {
    body = queueBody(a, 2, [a, b]);
    component = mount(AttentionDock, { target: host });
    await settle();
    dockBar().click();
    await settle();
    optionButton("Ship the twin").click();
    await settle();
    const inDrawer = () =>
      // The outgoing drawer can linger in jsdom; use the one holding focus.
      (document.activeElement?.closest("[data-adaptive-drawer]") ?? host).querySelector(
        'button[aria-label="Skip to next decision"]',
      ) as HTMLButtonElement;
    expect(inDrawer()).toBeTruthy();
    inDrawer().click();
    await settle();
    // The drawer stays open on the next item, focus lands in it.
    expect(host.querySelector(".dock-bar")).toBeNull();
    expect(document.activeElement?.textContent).toContain("Question B");
    inDrawer().click();
    await settle();
    // An outgoing drawer can linger in jsdom; the incoming chip is what matters.
    const chipA = Array.from(host.querySelectorAll(".decision-chip")).find((c) =>
      c.textContent?.includes("Question A"),
    ) as HTMLElement;
    expect(chipA).toBeTruthy();
    const kept = Array.from(chipA.querySelectorAll("button")).find((x) =>
      x.textContent?.includes("Ship the twin"),
    );
    expect(kept?.getAttribute("aria-pressed")).toBe("true");
  });

  it("hides Skip when a single item waits", async () => {
    body = queueBody(a, 1, [a]);
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(skipButton()).toBeNull();
    dockBar().click();
    await settle();
    expect(skipButton()).toBeNull();
    expect(host.textContent).not.toContain("1 of 1");
  });
});

describe("AttentionDock — a question that left the queue first", () => {
  async function answerAWhileServerDropsIt(detail: string) {
    answerStatus = () => ({ status: 410, payload: { detail } });
    const a = decisionItem({ id: "decision-a", title: "Question A" });
    const b = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(a, 2, [a, b]);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    // The server has already dropped A by the time the dock re-reads.
    body = queueBody(b, 1, [b]);
    answerButton().click();
    await settle();
  }

  it("drops a withdrawn question, says so in plain words, and moves to the next live item", async () => {
    await answerAWhileServerDropsIt("This question was withdrawn.");

    expect(answerBodies()).toHaveLength(1);
    expect(host.querySelectorAll(".decision-chip").length).toBe(0);
    expect(host.textContent).toContain("This question was withdrawn.");
    expect(dockBar().textContent).toContain("Question B");
    expect(host.textContent).not.toContain("Question A");
    expect(host.textContent).not.toMatch(/retry|couldn’t be saved|invalidated/i);
    const { answers } = await import("$lib/stores/attentionAnswerState");
    const { get } = await import("svelte/store");
    expect(get(answers)).toEqual({});
  });

  it("says an already answered question was already answered", async () => {
    await answerAWhileServerDropsIt("This was already answered.");

    expect(host.textContent).toContain("This was already answered.");
    expect(dockBar().textContent).toContain("Question B");
    expect(host.textContent).not.toContain("Question A");
  });

  it("drops the item even when the re-read still lists it", async () => {
    answerStatus = () => ({ status: 410, payload: { detail: "This question was withdrawn." } });
    const a = decisionItem({ id: "decision-a", title: "Question A" });
    const b = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(a, 2, [b]);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    answerButton().click();
    await settle();

    expect(dockBar().textContent).toContain("Question B");
    expect(host.textContent).not.toContain("Question A");
  });

  it("clears the sentence itself after a few seconds", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await answerAWhileServerDropsIt("This question was withdrawn.");
      expect(host.textContent).toContain("This question was withdrawn.");

      vi.advanceTimersByTime(8000);
      await settle();

      expect(host.textContent).not.toContain("This question was withdrawn.");
      expect(dockBar().textContent).toContain("Question B");
    } finally {
      vi.useRealTimers();
    }
  });

  it("falls back to a fixed sentence when the refusal carries no text", async () => {
    answerStatus = () => ({ status: 410, payload: {} });
    const a = decisionItem({ id: "decision-a", title: "Question A" });
    const b = decisionItem({ id: "decision-b", title: "Question B" });
    body = queueBody(a, 2, [a, b]);
    await openChip();
    optionButton("Ship the twin").click();
    await settle();
    body = queueBody(b, 1, [b]);
    answerButton().click();
    await settle();

    expect(host.textContent).toContain("This question is no longer open.");
  });
});
