/**
 * A decision raised by the thread being viewed opens on its own, unless the
 * person is busy, another chip is open or a lease holds the slot. "New" is the
 * server's raised_at against one per-view anchor; "busy" is read from the
 * document when the dock judges.
 */
import { flushSync, mount, tick, unmount } from "svelte";
import { get } from "svelte/store";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AttentionDock from "$lib/components/attention/AttentionDock.svelte";
import {
  __reset,
  collapse,
  EMPTY,
  expand,
  queue,
  reload,
  stale,
} from "$lib/stores/attentionDock.svelte";
import {
  forgetAutoOpen,
  judgeAutoOpen,
  noteLoad,
} from "$lib/stores/attentionAutoOpen";
import { token } from "$lib/stores/auth";
import { connectionState } from "$lib/stores/connection";
import * as productEvents from "$lib/product_events";
import { activeThreadId } from "$lib/stores/threads";
import type { AttentionQueue, AttentionQueueItem } from "$lib/api/attentionQueue";
import {
  SOURCE,
  decisionItem,
  externalReviewItem,
  queueBody,
} from "./fixtures/attentionQueue";

const VIEWED = "viewed-thread";

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;

/** A server timestamp `n` seconds past a fixed origin. */
const at = (n: number) => new Date(Date.UTC(2026, 9, 9, 12, 0, n)).toISOString();

function from(
  id: string,
  raised: number | null,
  thread = VIEWED,
): AttentionQueueItem {
  return decisionItem({
    id,
    title: `Question ${id}`,
    raised_at: raised === null ? null : at(raised),
    source: { ...SOURCE, thread_id: thread, thread_label: `Thread ${thread}` },
  });
}

function serve(...items: AttentionQueueItem[]) {
  body = queueBody(items[0] ?? null, items.length, items);
}

async function settle() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
    await tick();
    flushSync();
  }
}

/** The queue changes server-side and the dock learns of it, as `plans_changed` does. */
async function load(...items: AttentionQueueItem[]) {
  serve(...items);
  await reload();
  await settle();
}

async function mountDock(props: Record<string, unknown> = {}) {
  component = mount(AttentionDock, { target: host, props });
  await settle();
}

async function unmountDock() {
  unmount(component!);
  component = null;
  host.replaceChildren();
  await settle();
}

async function advance(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
  await settle();
}

/** A focusable text field, as the composer is. */
function field(tag: "textarea" | "input" | "composer"): HTMLElement {
  const el = document.createElement(tag === "composer" ? "div" : tag);
  if (tag === "composer") {
    el.setAttribute("contenteditable", "plaintext-only");
    el.tabIndex = 0;
  }
  host.appendChild(el);
  return el;
}

const chips = () => host.querySelectorAll(".decision-chip").length;
const keydown = () =>
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-10-09T12:00:00Z"));
  host = document.body;
  body = queueBody(null, 0);
  token.set("test-bearer");
  connectionState.set("connected");
  __reset();
  activeThreadId.set(VIEWED);
  vi.unstubAllGlobals();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
      headers: { get: () => null },
    })),
  );
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  host.replaceChildren();
  __reset();
  activeThreadId.set(null);
  connectionState.set("disconnected");
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("a decision from the viewed thread", () => {
  it("opens on its own when the person is idle, with no extra live region", async () => {
    await mountDock();
    await load();
    await load(from("d1", 10));

    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question d1");
    const announced = Array.from(host.querySelectorAll('[role="status"]')).filter((el) =>
      el.textContent?.startsWith("Decision from"),
    );
    expect(announced).toHaveLength(0);
  });

  it("opens the arrival even when it is not the queue head", async () => {
    const head = from("head", 1, "elsewhere");
    await mountDock();
    await load(head);
    await load(head, from("mine", 10));

    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question mine");
  });

  it("the first load for a thread opens nothing, however many are waiting", async () => {
    serve(from("old", 1), from("older", 2));
    await mountDock();
    await advance(5000);

    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();
  });

  it("a decision with no raised_at never opens", async () => {
    await mountDock();
    await load();
    await load(from("undated", null));
    expect(chips()).toBe(0);
  });

  it("records one product event per auto-open, none for a wait", async () => {
    const spy = vi.spyOn(productEvents, "emitProductEvent");
    await mountDock();
    await load();
    keydown();
    await load(from("d1", 10));
    expect(spy).not.toHaveBeenCalled();
    await advance(2000);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("attention_dock", "decision_auto_opened", {
      thread_id: VIEWED,
    });
  });

  it("an external review never opens on its own", async () => {
    await mountDock();
    await load();
    await load(externalReviewItem());
    expect(host.querySelector(".decision-chip")).toBeNull();
  });

  it("the oldest of several arrivals opens; the rest stay in the dock", async () => {
    await mountDock();
    await load();
    await load(from("a", 10), from("b", 11));
    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question a");

    collapse();
    await settle();
    await advance(3000);
    expect(chips()).toBe(0);
  });
});

describe("whose decisions never open", () => {
  it("a decision from another thread waits in the dock", async () => {
    await mountDock();
    await load();
    await load(from("d1", 10, "elsewhere"));
    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();
  });

  it("a hidden worker's decision carries the worker's thread and stays dock-only", async () => {
    await mountDock();
    await load();
    await load(from("d1", 10, "worker-of-viewed"));
    expect(chips()).toBe(0);
  });

  it("another decision's chip stays open: no swap now, no auto-open later", async () => {
    const first = from("first", 1);
    serve(first);
    await mountDock();
    expect(chips()).toBe(0);
    expand(first, false);
    await settle();
    await load(first, from("second", 10));
    expect(host.textContent).toContain("Question first");
    expect(host.textContent).not.toContain("Question second");

    collapse();
    await settle();
    await advance(3000);
    expect(chips()).toBe(0);
  });

  it("a collapsed auto-open never reopens, even when it leaves and returns", async () => {
    await mountDock();
    await load();
    await load(from("d1", 10));
    expect(chips()).toBe(1);

    collapse();
    await settle();
    await load(from("d1", 10));
    await load();
    await load(from("d1", 10));
    await advance(3000);
    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();
  });
});

describe("what was already there never opens", () => {
  it("switching away and back opens nothing that was pending meanwhile", async () => {
    await mountDock();
    await load();
    activeThreadId.set("other");
    await settle();
    await load(from("raised-while-away", 10));
    activeThreadId.set(VIEWED);
    await settle();
    await advance(3000);

    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();

    await load(from("raised-while-away", 10), from("fresh", 20));
    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question fresh");
  });

  it("signing out and back in opens nothing pre-existing", async () => {
    await mountDock();
    await load(from("d1", 10));
    token.set(null);
    await settle();
    serve(from("d1", 10), from("d2", 20));
    token.set("another-bearer");
    await settle();
    await advance(3000);
    expect(chips()).toBe(0);
  });

  it("a dock remount opens nothing raised while it was gone", async () => {
    await mountDock();
    await load();
    await unmountDock();

    await load(from("while-unmounted", 10));
    await mountDock();
    await advance(3000);
    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();

    await load(from("while-unmounted", 10), from("fresh", 20));
    expect(chips()).toBe(1);
  });

  it("decisions raised during a disconnect open after reconnect: that is intended", async () => {
    await mountDock();
    await load();
    connectionState.set("disconnected");
    await settle();
    serve(from("during-outage", 10));
    connectionState.set("connected");
    await settle();
    expect(chips()).toBe(1);
  });
});

describe("while the person is busy", () => {
  /** Types a draft into the field the way a person would, without a keydown the dock saw. */
  const setDraft = (el: HTMLElement, text: string) => {
    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) el.value = text;
    else el.textContent = text;
  };

  it.each(["textarea", "input", "composer"] as const)(
    "a focused empty %s does not hold it: the composer keeps focus after a send",
    async (kind) => {
      const el = field(kind);
      await mountDock();
      await load();
      el.focus();
      await advance(0);
      await load(from("d1", 10));
      await advance(2000);
      expect(chips()).toBe(1);
    },
  );

  it.each(["textarea", "input", "composer"] as const)(
    "a focused %s with an unsent draft holds it, and leaving the field releases it",
    async (kind) => {
      const el = field(kind);
      const elsewhere = document.createElement("button");
      host.appendChild(elsewhere);
      await mountDock();
      await load();
      el.focus();
      setDraft(el, "half a thought");
      await advance(0);
      await load(from("d1", 10));
      await advance(10000);
      expect(chips()).toBe(0);

      elsewhere.focus();
      await advance(0);
      expect(chips()).toBe(1);
    },
  );

  it("a blank draft is not a draft", async () => {
    const el = field("composer");
    await mountDock();
    await load();
    el.focus();
    setDraft(el, "  \n ");
    await advance(0);
    await load(from("d1", 10));
    await advance(2000);
    expect(chips()).toBe(1);
  });

  it("sending the draft releases it once the typing window has passed", async () => {
    const el = field("composer");
    await mountDock();
    await load();
    el.focus();
    setDraft(el, "hello");
    keydown();
    await load(from("d1", 10));
    expect(chips()).toBe(0);

    // Enter sends: the composer clears but keeps focus.
    keydown();
    setDraft(el, "");
    await advance(1999);
    expect(chips()).toBe(0);
    await advance(1);
    expect(chips()).toBe(1);
  });

  it("a draft present when the decision arrives holds it; sending it releases it after the window", async () => {
    const el = field("composer");
    await mountDock();
    await load();
    el.focus();
    setDraft(el, "hello");
    await advance(5000);
    await load(from("d1", 10));
    await advance(5000);
    expect(chips()).toBe(0);

    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    setDraft(el, "");
    await advance(1999);
    expect(chips()).toBe(0);
    await advance(1);
    expect(chips()).toBe(1);
  });

  it("a draft deleted by an input event with no keydown releases it after the window", async () => {
    const el = field("composer");
    await mountDock();
    await load();
    el.focus();
    setDraft(el, "hello");
    await advance(5000);
    await load(from("d1", 10));
    await advance(5000);
    expect(chips()).toBe(0);

    setDraft(el, "");
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await advance(1999);
    expect(chips()).toBe(0);
    await advance(1);
    expect(chips()).toBe(1);
  });

  it("a draft that stays keeps holding it", async () => {
    const el = field("composer");
    await mountDock();
    await load();
    el.focus();
    setDraft(el, "hello");
    await advance(5000);
    await load(from("d1", 10));
    keydown();
    await advance(60000);
    expect(chips()).toBe(0);
  });

  it("a focused button or checkbox does not hold it", async () => {
    const box = document.createElement("input");
    box.type = "checkbox";
    host.appendChild(box);
    await mountDock();
    await load();
    box.focus();
    await advance(0);
    await load(from("d1", 10));
    expect(chips()).toBe(1);
  });

  it("a keydown within the last 2 s holds it, then it opens", async () => {
    await mountDock();
    await load();
    keydown();
    await load(from("d1", 10));
    expect(chips()).toBe(0);
    await advance(1999);
    expect(chips()).toBe(0);
    await advance(1);
    expect(chips()).toBe(1);
  });

  it("a further keydown restarts the window", async () => {
    await mountDock();
    await load();
    keydown();
    await load(from("d1", 10));
    await advance(1500);
    keydown();
    await advance(1500);
    expect(chips()).toBe(0);
    await advance(500);
    expect(chips()).toBe(1);
  });

  it("another text field mounting or unmounting changes nothing", async () => {
    await mountDock();
    await load();
    const second = field("composer");
    second.remove();
    await advance(0);
    await load(from("d1", 10));
    expect(chips()).toBe(1);
  });

  it("is dropped if the person leaves the thread and returns", async () => {
    await mountDock();
    await load();
    keydown();
    await load(from("d1", 10));
    activeThreadId.set("other");
    await advance(3000);
    activeThreadId.set(VIEWED);
    await advance(3000);
    expect(chips()).toBe(0);
    expect(host.querySelector(".dock-bar")).toBeTruthy();
  });

  it("is dropped if it is answered elsewhere before it opens", async () => {
    await mountDock();
    await load();
    keydown();
    await load(from("d1", 10));
    await load();
    await advance(3000);
    expect(chips()).toBe(0);
  });
});

describe("the open itself", () => {
  it("moves focus into the drawer; Escape closes it and returns focus to where it was", async () => {
    const before = document.createElement("button");
    host.appendChild(before);
    await mountDock();
    await load();
    before.focus();
    await advance(0);
    await load(from("d1", 10));
    await advance(0);

    expect(chips()).toBe(1);
    const drawer = host.querySelector('[role="dialog"]') as HTMLElement;
    expect(drawer.contains(document.activeElement)).toBe(true);

    drawer.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(chips()).toBe(0);
    expect(document.activeElement).toBe(before);
  });

  it("with nothing focused beforehand, Escape falls back to the dock bar", async () => {
    await mountDock();
    await load();
    await load(from("d1", 10));
    await advance(0);

    const drawer = host.querySelector('[role="dialog"]') as HTMLElement;
    drawer.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(document.activeElement).toBe(host.querySelector(".dock-bar"));
  });
});

describe("loads and thread changes", () => {
  it("switching threads anchors from the queue already held, with no fetch", async () => {
    await mountDock();
    await load(from("existing", 1, "other"));
    const fetchSpy = vi.mocked(fetch);
    fetchSpy.mockClear();

    activeThreadId.set("other");
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();

    // Anchored on "other" from the held queue: its existing decision never opens,
    // a later one does.
    await advance(3000);
    expect(chips()).toBe(0);
    await load(from("existing", 1, "other"), from("later", 10, "other"));
    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question later");
  });

  it("a thread set before the first load lands anchors on that load, not on nothing", async () => {
    activeThreadId.set(null);
    // The first load carries a decision that was already pending, and the thread
    // is set before it lands.
    serve(from("pending", 5));
    component = mount(AttentionDock, { target: host, props: {} });
    flushSync();
    activeThreadId.set(VIEWED);
    flushSync();
    expect(get(queue)).toBe(EMPTY);
    await settle();
    await advance(3000);
    expect(chips()).toBe(0);

    await load(from("pending", 5), from("later", 10));
    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question later");
  });

  it("a thread switch right after sign-in, before the reload lands, anchors on that reload", async () => {
    await mountDock();
    await load(from("old", 1));
    token.set(null);
    await settle();
    serve(from("pending", 5));
    activeThreadId.set("other");
    await settle();
    activeThreadId.set(VIEWED);
    await settle();
    token.set("another-bearer");
    await settle();
    await advance(3000);
    expect(chips()).toBe(0);

    await load(from("pending", 5), from("later", 10));
    expect(chips()).toBe(1);
    expect(host.textContent).toContain("Question later");
  });

  it("a response without an items array is a failed load: stale, queue unchanged", async () => {
    await mountDock();
    await load(from("kept", 1));
    expect(get(stale)).toBe(false);

    body = { current: null, count: 0 } as unknown as AttentionQueue;
    await reload();
    await settle();

    expect(get(stale)).toBe(true);
    expect(get(queue).items.map((i) => i.external_id)).toEqual(["kept"]);
  });
});

describe("the judge", () => {
  const idle = { expandedId: null, suppressed: false, busy: false } as const;

  it("a lease holds it, and it opens once the lease clears", () => {
    noteLoad([], VIEWED);
    const items = [from("d1", 10)];
    const held = judgeAutoOpen(items, { ...idle, viewedThreadId: VIEWED, suppressed: true });
    expect(held.state).toBe("wait");
    const free = judgeAutoOpen(items, { ...idle, viewedThreadId: VIEWED });
    expect(free.state).toBe("open");
    // Offered once: the anchor moved.
    expect(judgeAutoOpen(items, { ...idle, viewedThreadId: VIEWED }).state).toBe("none");
  });

  it("an equal or older raised_at is not new", () => {
    noteLoad([from("seen", 10)], VIEWED);
    const verdict = judgeAutoOpen([from("seen", 10), from("older", 5)], {
      ...idle,
      viewedThreadId: VIEWED,
    });
    expect(verdict.state).toBe("none");
  });

  it("judges nothing before a load has anchored the viewed thread", () => {
    forgetAutoOpen();
    expect(
      judgeAutoOpen([from("d1", 10)], { ...idle, viewedThreadId: VIEWED }).state,
    ).toBe("none");
  });
});
