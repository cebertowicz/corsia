/**
 * A held reply in the attention dock: the same bar and drawer as a decision, drawn from its own
 * detail, deciding through `/api/held-replies`.
 */
import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AttentionDock from "$lib/components/attention/AttentionDock.svelte";
import { __reset } from "$lib/stores/attentionDock.svelte";
import { answerNotice } from "$lib/stores/attentionAnswerState";
import { token, user } from "$lib/stores/auth";
import { connectionState } from "$lib/stores/connection";
import { emit, on } from "$lib/ws/events";
import { get } from "svelte/store";
import type { AttentionQueue } from "$lib/api/attentionQueue";
import { decisionItem, heldReplyItem, queueBody } from "./fixtures/attentionQueue";

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;
let calls: Array<{ url: string; init?: RequestInit }>;
let decide: () => { status: number; payload?: unknown } | Promise<{ status: number; payload?: unknown }>;

function respond(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
    headers: { get: () => null },
  };
}

async function settle() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
    await tick();
    flushSync();
  }
}

function button(name: string): HTMLButtonElement | undefined {
  return Array.from(host.querySelectorAll("button")).find(
    (item) => (item.textContent ?? "").trim() === name,
  ) as HTMLButtonElement | undefined;
}

async function openDock() {
  component = mount(AttentionDock, { target: host });
  await settle();
  (host.querySelector(".dock-bar") as HTMLButtonElement).click();
  await settle();
}

function decisions(): Array<{ url: string; authorization: string | null; body: BodyInit | null | undefined }> {
  return calls
    .filter((call) => call.url.includes("/api/held-replies/"))
    .map((call) => ({ url: call.url, authorization: new Headers(call.init?.headers).get("Authorization"), body: call.init?.body }));
}

beforeEach(() => {
  host = document.body;
  calls = [];
  body = queueBody(heldReplyItem(), 1);
  decide = () => ({ status: 200, payload: { ok: true, state: "sent" } });
  token.set("test-bearer");
  connectionState.set("connected");
  __reset();
  vi.unstubAllGlobals();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      if (String(url).includes("/api/attention/queue")) return respond(200, body);
      const outcome = await decide();
      return respond(outcome.status, outcome.payload ?? {});
    }),
  );
});
afterEach(() => {
  if (component) unmount(component);
  component = null;
  host.replaceChildren();
  __reset();
  connectionState.set("disconnected");
  vi.restoreAllMocks();
});

describe("held reply in the dock", () => {
  it("shows the bar with the word Review and the drawer with the exact text, no options", async () => {
    component = mount(AttentionDock, { target: host });
    await settle();
    expect(host.querySelector(".dock-action")?.textContent?.trim()).toBe("Review");
    (host.querySelector(".dock-bar") as HTMLButtonElement).click();
    await settle();
    expect(host.textContent).toContain("Approving sends the exact text below to that cloud provider.");
    const well = host.querySelector(".well") as HTMLElement;
    expect(well.textContent).toBe("First line of the reply.\n\n  Indented second paragraph.");
    expect(button("Approve once")).toBeTruthy();
    expect(button("Decline")).toBeTruthy();
    expect(button("Answer")).toBeUndefined();
    expect(host.querySelector(".option-button, .option-row, textarea")).toBeNull();
    expect(button("Skip")).toBeUndefined();
  });

  it("names the drawer 'Held reply' alone, and by position once more than one item waits", async () => {
    await openDock();
    expect(host.textContent).toContain("Held reply · Sender Thread · Project One");
    unmount(component!);
    component = null;
    host.replaceChildren();
    __reset();
    body = queueBody(heldReplyItem(), 2, [heldReplyItem(), decisionItem()]);
    await openDock();
    expect(host.textContent).toContain("1 of 2 · Sender Thread · Project One");
    expect(button("Skip")?.getAttribute("aria-label")).toBe("Skip to next item");
  });

  it("calls a plan decision's Skip 'next item' only while a held reply is queued", async () => {
    body = queueBody(decisionItem(), 2, [decisionItem(), heldReplyItem()]);
    await openDock();
    expect(button("Skip")?.getAttribute("aria-label")).toBe("Skip to next item");
    unmount(component!);
    component = null;
    host.replaceChildren();
    __reset();
    body = queueBody(decisionItem(), 2, [decisionItem(), decisionItem({ id: "decision-2" })]);
    await openDock();
    expect(button("Skip")?.getAttribute("aria-label")).toBe("Skip to next decision");
  });

  it("approves once: posts the token, shows the not-yet-sent line while it is in flight, then leaves", async () => {
    let release!: () => void;
    decide = () => new Promise((resolve) => { release = () => resolve({ status: 200, payload: { ok: true, state: "sent" } }); });
    await openDock();
    button("Approve once")!.click();
    await settle();
    expect(decisions()).toEqual([{ url: "/api/held-replies/held-1/approve", authorization: "Bearer test-bearer", body: undefined }]);
    expect(host.querySelector(".dock-receipt")?.textContent).toBe(
      "The reply to the Researcher agent is approved and not yet sent.",
    );
    body = queueBody(null, 0);
    release();
    await settle();
    expect(host.querySelector(".dock-receipt")).toBeNull();
    expect(host.querySelector(".dock-bar")).toBeNull();
  });

  it.each([
    ["Approve once", true], ["Decline", true],
    ["Approve once", false], ["Decline", false],
  ])("keeps the closing drawer through %s with next item %s", async (action, hasNext) => {
    body = hasNext ? queueBody(heldReplyItem(), 2, [heldReplyItem(), decisionItem()]) : queueBody(heldReplyItem(), 1);
    decide = () => new Promise(() => {});
    await openDock();
    const drawer = host.querySelector(".drawer-motion") as HTMLElement;
    expect(drawer).toBeTruthy();
    const outroStarted = vi.fn();
    drawer.addEventListener("outrostart", outroStarted);
    button(action)!.click();
    await tick();
    flushSync();
    expect(outroStarted).toHaveBeenCalledOnce();
    expect(decisions()).toHaveLength(1);
    expect(host.querySelector(".dock-receipt")).toBeTruthy();
    expect(host.querySelector(".drawer-motion")).toBe(drawer);
    expect(host.querySelector(".dock-bar") !== null).toBe(hasNext);
    drawer.dispatchEvent(new Event("outroend"));
    await settle();
    expect(host.querySelector(".drawer-motion")).toBeNull();
  });

  function addComposer(): HTMLElement {
    const composer = document.createElement("div");
    composer.setAttribute("role", "textbox");
    composer.setAttribute("aria-label", "Message CORSIA");
    composer.tabIndex = 0;
    host.append(composer);
    return composer;
  }

  /** The decision request waits until the test settles it, so focus can be read mid-flight. */
  function heldDecision(): (outcome: { status: number; payload?: unknown }) => void {
    let release: (outcome: { status: number; payload?: unknown }) => void = () => {};
    const pending = new Promise<{ status: number; payload?: unknown }>((resolve) => { release = resolve; });
    decide = () => pending;
    return release;
  }

  it.each(["Approve once", "Decline"])("returns focus to the dock once %s is recorded", async (action) => {
    body = queueBody(heldReplyItem(), 2, [heldReplyItem(), decisionItem()]);
    const release = heldDecision();
    await openDock();
    button(action)!.focus();
    button(action)!.click();
    await settle();
    expect(document.activeElement).not.toBe(host.querySelector(".dock-bar"));
    body = queueBody(decisionItem(), 1);
    release({ status: 200, payload: { ok: true, state: action === "Decline" ? "declined" : "sent" } });
    await settle();
    expect(document.activeElement).toBe(host.querySelector(".dock-bar"));
  });

  it.each(["Approve once", "Decline"])("returns focus to the composer after the last held reply: %s", async (action) => {
    const composer = addComposer();
    const release = heldDecision();
    await openDock();
    button(action)!.focus();
    button(action)!.click();
    await settle();
    expect(document.activeElement).not.toBe(composer);
    body = queueBody(null, 0);
    release({ status: 200, payload: { ok: true, state: action === "Decline" ? "declined" : "sent" } });
    await settle();
    expect(document.activeElement).toBe(composer);
  });

  it("keeps focus off the composer when a failed send reopens the reply, and lands on its error", async () => {
    const composer = addComposer();
    const line = "Couldn’t send the reply to the Researcher agent because of an unexpected error. Approve once to try again.";
    const release = heldDecision();
    await openDock();
    button("Approve once")!.focus();
    button("Approve once")!.click();
    await settle();
    expect(document.activeElement).not.toBe(composer);
    release({ status: 502, payload: { detail: line } });
    await settle();
    expect(host.querySelector(".problem")?.textContent).toBe(line);
    expect(document.activeElement).toBe(host.querySelector("h3[data-drawer-autofocus]"));
  });

  it("keeps focus off the composer when the recipient is gone, and lands on that line", async () => {
    const composer = addComposer();
    const line = "Couldn’t send the reply because the Researcher agent no longer exists.";
    const release = heldDecision();
    await openDock();
    button("Approve once")!.focus();
    button("Approve once")!.click();
    await settle();
    expect(document.activeElement).not.toBe(composer);
    body = queueBody(null, 0);
    release({ status: 200, payload: { ok: true, state: "not_sent", reason: "recipient_gone", line } });
    await settle();
    expect(document.activeElement).toBe(host.querySelector("h3.problem"));
  });

  it("leaves focus where the person put it while the decision was in flight", async () => {
    const composer = addComposer();
    const elsewhere = document.createElement("button");
    host.append(elsewhere);
    const release = heldDecision();
    await openDock();
    button("Approve once")!.focus();
    button("Approve once")!.click();
    await settle();
    elsewhere.focus();
    body = queueBody(null, 0);
    release({ status: 200, payload: { ok: true, state: "sent" } });
    await settle();
    expect(document.activeElement).toBe(elsewhere);
    expect(document.activeElement).not.toBe(composer);
  });


  it("declines: posts decline, and the dock says the reply was not sent", async () => {
    decide = () => ({ status: 200, payload: { ok: true, state: "declined" } });
    await openDock();
    button("Decline")!.click();
    body = queueBody(null, 0);
    await settle();
    expect(decisions()[0].url).toBe("/api/held-replies/held-1/decline");
    expect(get(answerNotice)).toBe("The reply to the Researcher agent was not sent.");
  });

  it("shows a terminal drawer when the recipient no longer exists", async () => {
    const line = "Couldn’t send the reply because the Researcher agent no longer exists.";
    decide = () => ({ status: 200, payload: { ok: true, state: "not_sent", reason: "recipient_gone", line } });
    await openDock();
    button("Approve once")!.click();
    body = queueBody(null, 0);
    await settle();
    expect(host.querySelector("h3.problem")?.textContent).toBe(line);
    expect(button("Approve once")).toBeUndefined();
    expect(button("Decline")).toBeUndefined();
    expect(button("Done")).toBeTruthy();
    expect(get(answerNotice)).toBeNull();
    button("Done")!.click();
    await settle();
    expect(host.querySelector("h3.problem")).toBeNull();
  });

  it("keeps the notice when sending is refused by scope", async () => {
    const line = "Couldn’t send the reply because the Researcher agent is no longer reachable from this chat.";
    decide = () => ({ status: 200, payload: { ok: true, state: "not_sent", reason: "scope", line } });
    await openDock();
    button("Approve once")!.click();
    body = queueBody(null, 0);
    await settle();
    expect(get(answerNotice)).toBe(line);
    expect(host.querySelector("h3.problem")).toBeNull();
  });

  it.each(["Approve once", "Decline"])("returns to sign-in when %s gets a 401", async (action) => {
    const wsError = vi.fn();
    const offError = on("error", wsError);
    user.set({ id: "u-test", name: "Test" });
    decide = () => ({ status: 401 });
    await openDock();
    button(action)!.click();
    await settle();
    expect(get(token)).toBeNull();
    expect(get(user)).toBeNull();
    expect(wsError).not.toHaveBeenCalled();
    offError();
  });

  it("returns the reply, reopened, with the server's error line when delivery fails, and Approve once retries", async () => {
    const line = "Couldn’t send the reply to the Researcher agent because of an unexpected error. Approve once to try again.";
    decide = () => ({ status: 502, payload: { detail: line, reference: "K7QM2X" } });
    await openDock();
    button("Approve once")!.click();
    await settle();
    expect(host.querySelector(".problem")?.textContent).toBe(line);
    expect(host.querySelector(".reference")?.textContent).toBe("Reference: K7QM2X");
    expect(button("Approve once")!.disabled).toBe(false);
    decide = () => ({ status: 200, payload: { ok: true, state: "sent" } });
    button("Approve once")!.click();
    await settle();
    expect(decisions().map((call) => call.url)).toEqual([
      "/api/held-replies/held-1/approve",
      "/api/held-replies/held-1/approve",
    ]);
  });

  it("shows a stored error above the text", async () => {
    const line = "Couldn’t send the reply to the Researcher agent because of an unexpected error. Approve once to try again.";
    body = queueBody(heldReplyItem({ error: line, error_ref: "K7QM2X" }), 1);
    await openDock();
    expect(host.querySelector(".problem")?.textContent).toBe(line);
    expect(host.querySelector(".reference")?.textContent).toBe("Reference: K7QM2X");
  });

  it("drops a reply another page already decided, with no error", async () => {
    decide = () => ({ status: 409, payload: { detail: "This reply was already decided" } });
    await openDock();
    button("Approve once")!.click();
    body = queueBody(null, 0);
    await settle();
    expect(host.querySelector(".problem")).toBeNull();
    expect(host.querySelector(".dock-bar")).toBeNull();
  });

  it("pauses both buttons offline and says so", async () => {
    await openDock();
    connectionState.set("disconnected");
    await settle();
    expect(button("Approve once")!.disabled).toBe(true);
    expect(button("Decline")!.disabled).toBe(true);
    expect(host.textContent).toContain("Approve once and Decline are paused until this reconnects.");
  });

  it("re-reads the queue when a held-reply change is announced", async () => {
    component = mount(AttentionDock, { target: host });
    await settle();
    const before = calls.filter((call) => call.url.includes("/api/attention/queue")).length;
    emit("held_reply_changed", { thread_ids: ["child"] });
    await settle();
    expect(calls.filter((call) => call.url.includes("/api/attention/queue")).length).toBeGreaterThan(before);
  });

  it("says 'Review this' under a host-access request", async () => {
    component = mount(AttentionDock, { target: host, props: { suppressed: true } });
    await settle();
    expect(host.querySelector(".dock-deferred")?.textContent?.trim()).toBe(
      "Review this after the host-access request above.",
    );
  });
});
