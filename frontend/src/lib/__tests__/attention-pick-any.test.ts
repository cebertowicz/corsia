/**
 * Pick-any decisions in the dock: independent checkboxes, the recommendation
 * pre-ticked, a note that never unticks anything, and none as a valid answer.
 * Pick one is covered, unchanged, by `attention-dock.test.ts`.
 */
import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AttentionDock from "$lib/components/attention/AttentionDock.svelte";
import { __reset } from "$lib/stores/attentionDock.svelte";
import { token } from "$lib/stores/auth";
import { connectionState } from "$lib/stores/connection";
import { answerDecision, recommendedIds, type AttentionQueue } from "$lib/api/attentionQueue";
import { pickAnyDetail, pickAnyItem, queueBody } from "./fixtures/attentionQueue";

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
      if (String(url).includes("/api/attention/queue")) return respond(200, body);
      attempt += 1;
      const outcome = answerStatus(attempt);
      return respond(outcome.status, outcome.payload ?? { decision: { state: "answered" } });
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

function boxes(): HTMLInputElement[] {
  return Array.from(host.querySelectorAll("fieldset input[type=checkbox]"));
}

function box(label: string): HTMLInputElement {
  const match = boxes().find((item) => item.getAttribute("aria-label")?.startsWith(label));
  expect(match, label).toBeTruthy();
  return match as HTMLInputElement;
}

function answerButton(label = "answer"): HTMLButtonElement {
  const match = Array.from(host.querySelectorAll("button")).find(
    (item) => (item.textContent ?? "").trim().toLowerCase() === label,
  );
  expect(match).toBeTruthy();
  return match as HTMLButtonElement;
}

function note(): HTMLTextAreaElement {
  const el = host.querySelector(".other-option textarea") as HTMLTextAreaElement;
  expect(el).toBeTruthy();
  return el;
}

function typeNote(text: string) {
  const el = note();
  el.value = text;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function receipt(): string {
  return (host.querySelector(".receipt")?.textContent ?? "").trim();
}

function answerBodies(): Array<Record<string, unknown>> {
  return calls
    .filter((call) => call.url.includes("/answer"))
    .map((call) => JSON.parse(call.init!.body as string));
}

async function openChip(props: Record<string, unknown> = {}) {
  component = mount(AttentionDock, { target: host, props });
  await settle();
  // After a remount the dock may still be expanded: there is no bar to open.
  (host.querySelector(".dock-bar") as HTMLButtonElement | null)?.click();
  await settle();
}

async function tickBox(label: string) {
  box(label).click();
  await settle();
}

beforeEach(() => {
  // The drawer portals to document.body, so the host is the body itself.
  host = document.body;
  calls = [];
  body = queueBody(pickAnyItem(), 1);
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
  vi.restoreAllMocks();
});

describe("pick any — what the person sees", () => {
  it("S1: groups native checkboxes in a fieldset, with the recommendation pre-ticked", async () => {
    await openChip();

    expect(host.querySelector("fieldset legend")?.textContent).toBe("Choose any that apply");
    expect(boxes()).toHaveLength(3);
    expect(boxes().map((item) => item.checked)).toEqual([true, true, false]);
    expect(host.querySelectorAll(".recommended").length).toBe(2);
    expect(host.textContent).toContain("Recommended");
    expect(receipt()).toBe("2 of 3 chosen · as recommended");
    expect(answerButton().disabled).toBe(false);
    expect(host.querySelector(".option-button")).toBeNull();
  });

  it("names each checkbox by its label, plus 'recommended', and describes the first recommendation by its detail and rationale", async () => {
    await openChip();

    const first = box("Bring over your MCP servers (3)");
    expect(first.getAttribute("aria-label")).toBe("Bring over your MCP servers (3), recommended");
    expect(box("Daily summary").getAttribute("aria-label")).toBe("Daily summary");
    const describedBy = first.getAttribute("aria-describedby")!.split(" ");
    expect(describedBy).toHaveLength(2);
    expect(document.getElementById(describedBy[0])?.textContent).toBe("Copies your config.");
    expect(document.getElementById(describedBy[1])?.textContent).toContain("None of them changes anything else on this Mac.");
    expect(first.closest("label")).toBeTruthy();
    expect(first.tabIndex).toBe(0);
  });

  it("S2: a partial set reads k of n, without 'as recommended'", async () => {
    await openChip();
    await tickBox("Web search");

    expect(receipt()).toBe("1 of 3 chosen");
  });

  it("S3: nothing ticked and no note says what answering means", async () => {
    await openChip();
    await tickBox("Bring over");
    await tickBox("Web search");

    expect(receipt()).toBe("None chosen. Answering tells the agent you want none of these.");
    expect(answerButton().disabled).toBe(false);
  });

  it("S4 and S5: a note changes the receipt, and never unticks anything", async () => {
    await openChip();
    typeNote("later this week");
    await settle();
    expect(note().classList.contains("selected")).toBe(false);
    expect(boxes().map((item) => item.checked)).toEqual([true, true, false]);
    expect(receipt()).toBe("2 of 3 chosen · as recommended");

    await tickBox("Daily summary");
    expect(receipt()).toBe("3 of 3 chosen");

    for (const label of ["Bring over", "Web search", "Daily summary"]) await tickBox(label);
    expect(receipt()).toBe("None chosen, with a note.");
  });

  it("S6: an empty recommendation starts with nothing ticked and no Recommended mark", async () => {
    body = queueBody(pickAnyItem({ recommendation: [] }), 1);
    await openChip();

    expect(boxes().map((item) => item.checked)).toEqual([false, false, false]);
    expect(host.querySelectorAll(".recommended").length).toBe(0);
    expect(receipt()).toBe("None chosen. Answering tells the agent you want none of these.");
  });

  it("labels the note, and unticking then re-ticking the same set counts as recommended again", async () => {
    await openChip();
    expect(note().closest("label")?.textContent).toContain("Note");
    expect(note().placeholder).toBe("Optional: anything else the agent should know");

    await tickBox("Web search");
    await tickBox("Web search");
    expect(receipt()).toBe("2 of 3 chosen · as recommended");
  });
});

describe("pick any — what is sent", () => {
  it("sends the untouched recommendation as option_ids, with no note", async () => {
    await openChip();
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(sent.option_ids).toEqual(["A", "B"]);
    expect(sent).not.toHaveProperty("option_id");
    expect(sent.other_text).toBeNull();
    expect(sent.expected_revision).toBe(3);
    expect(typeof sent.mutation_id).toBe("string");
  });

  it("sends the ticked set in option order, and the note whenever it is non-blank", async () => {
    await openChip();
    await tickBox("Web search");
    await tickBox("Daily summary");
    typeNote("  do search later  ");
    await settle();
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(sent.option_ids).toEqual(["A", "C"]);
    expect(sent.other_text).toBe("do search later");
  });

  it("sends an explicit none as an empty list", async () => {
    await openChip();
    await tickBox("Bring over");
    await tickBox("Web search");
    answerButton().click();
    await settle();

    const [sent] = answerBodies();
    expect(sent.option_ids).toEqual([]);
  });

  it("a pick-one answer still sends option_id and no option_ids", async () => {
    await answerDecision(
      "test-bearer",
      "decision-1",
      { optionId: "A", otherText: "", expectedRevision: 3 },
      "m-1",
    );

    const [sent] = answerBodies();
    expect(sent).toEqual({
      option_id: "A",
      other_text: null,
      expected_revision: 3,
      mutation_id: "m-1",
    });
  });
});

describe("pick any — failure, offline and navigation", () => {
  it("S8: a failed send returns with the ticks and note restored, and Retry", async () => {
    answerStatus = () => ({ status: 500 });
    await openChip();
    await tickBox("Daily summary");
    typeNote("note kept");
    await settle();
    answerButton().click();
    await settle();

    expect(boxes().map((item) => item.checked)).toEqual([true, true, true]);
    expect(note().value).toBe("note kept");
    expect(receipt()).toMatch(/couldn’t/i);
    expect(answerButton("retry").disabled).toBe(false);
  });

  it("S9: offline disables every checkbox and the note, and says why", async () => {
    await openChip({ writesDisabled: true });

    expect(boxes().every((item) => item.disabled)).toBe(true);
    expect(note().disabled).toBe(true);
    expect(answerButton().disabled).toBe(true);
    expect(host.textContent).toContain("Answering is paused until this reconnects.");
  });

  it("S11: the ticks and note survive leaving and reopening the chip", async () => {
    await openChip();
    await tickBox("Web search");
    typeNote("kept across routes");
    await settle();

    unmount(component!);
    component = null;
    await openChip();

    expect(boxes().map((item) => item.checked)).toEqual([true, false, false]);
    expect(note().value).toBe("kept across routes");
  });

  it("S13: an old client's option_id answer is refused with the reload sentence", async () => {
    answerStatus = () => ({
      status: 422,
      payload: { detail: "This decision lets you choose several options. Reload to answer it." },
    });
    await openChip();
    answerButton().click();
    await settle();

    expect(receipt()).toBe("This decision lets you choose several options. Reload to answer it.");
  });

  it("S14: four long labels stay inside their rows", async () => {
    const long = "A deliberately long option label that has to wrap inside its own row ".repeat(2).trim();
    body = queueBody(
      pickAnyItem({
        options: ["A", "B", "C", "D"].map((id) => ({ id, label: `${id} ${long}`, detail: long })),
        recommendation: ["A"],
      }),
      1,
    );
    await openChip();

    expect(boxes()).toHaveLength(4);
    expect(host.querySelectorAll(".option-row")).toHaveLength(4);
  });
});

describe("pick any — helpers", () => {
  it("reads the recommendation of either kind in option order", () => {
    expect(recommendedIds(pickAnyDetail({ recommendation: ["B", "A"] }))).toEqual(["A", "B"]);
    expect(recommendedIds(pickAnyDetail({ recommendation: [] }))).toEqual([]);
    expect(
      recommendedIds(pickAnyDetail({ pick: "one", recommendation: "C" })),
    ).toEqual(["C"]);
  });
});
