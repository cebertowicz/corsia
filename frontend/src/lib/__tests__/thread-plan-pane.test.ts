import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ThreadPlanPane from "$lib/components/plans/ThreadPlanPane.svelte";
import PlanHeadControls from "$lib/components/plans/PlanHeadControls.svelte";
import headSource from "$lib/components/plans/PlanHeadControls.svelte?raw";
import { resetPlanRuntimeForTests } from "$lib/stores/plans.svelte";
import { resetObserversForTests } from "$lib/stores/planQuery.svelte";
import { token } from "$lib/stores/auth";
import { connectionState } from "$lib/stores/connection";
import type { OperatorEnvelope } from "$lib/api/plans";
import { decisionItem, queueBody } from "./fixtures/attentionQueue";
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from "./fixtures/operatorPlan";
import {
  blockedPlanTree,
  pausedPlanTree,
  type PlanTreeShape,
} from "./fixtures/blockedPlanTree";

/** Past the old 45 s refresh loop: a timer-driven read would land inside it. */
const NO_POLL_WINDOW_MS = 60_000;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let head: Record<string, unknown> | null = null;
let wsListeners: Map<string, (data: unknown) => void>;

vi.mock("$lib/ws", () => ({
  on: (event: string, cb: (data: unknown) => void) => {
    wsListeners.set(event, cb);
    return () => wsListeners.delete(event);
  },
}));

function envelope(
  threadId: string,
  over: Partial<OperatorEnvelope["plans"][number]["progress"]> = {},
): OperatorEnvelope {
  return {
    contract_version: 2,
    as_of: "now",
    target: { kind: "thread", id: threadId, label: "Thread" },
    revision_token: `rev-${threadId}`,
    attention: { awaiting_count: 0 },
    plans: [
      {
        plan: {
          id: `plan-${threadId}`,
          owner_kind: "thread",
          owner_id: threadId,
          state: "active",
          revision: 1,
          updated_at: "now",
        },
        owner: { label: "Thread", project_id: null, thread_id: threadId },
        progress: { done: 1, total: 2, open: 1, liveness: "advancing", distribution: { ...EMPTY_DISTRIBUTION, active: 1 }, ...over },
        display: planDisplay("working", "active"),
        oversize: null,
        current_node_ids: ["step-1"],
        next_node_ids: [],
        folds: [],
        root_count: 1,
        nodes: [operatorNode({
          id: "step-1",
          title: `Step for ${threadId}`,
          state: "active",
          executor: { thread_id: threadId, label: "Thread" },
        })],
      },
    ],
  };
}

function routedFetch(routes: Record<string, () => unknown>) {
  const calls: string[] = [];
  const fn = vi.fn(async (url: string) => {
    calls.push(url);
    const key = Object.keys(routes).find((r) => url.includes(r));
    if (!key)
      return {
        ok: false,
        status: 404,
        json: async () => null,
        headers: { get: () => null },
      };
    return routes[key]();
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

function ok(body: unknown, etag = '"e1"') {
  return {
    ok: true,
    status: 200,
    json: async (): Promise<unknown> => body,
    headers: { get: (k: string) => (k === "ETag" ? etag : null) },
  };
}

function mockFetch(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
) {
  const response = {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
    text: () =>
      Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
    headers: { get: (key: string) => headers[key] ?? null },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
}

/** The pane plus the count and Pause its host header renders (section cap or drawer). */
function mountWithHead(threadId: string) {
  const cap = document.createElement("div");
  cap.className = "plan-cap";
  host.appendChild(cap);
  head = mount(PlanHeadControls, { target: cap, props: { threadId } });
  return mount(ThreadPlanPane, { target: host, props: { threadId } });
}

async function settle() {
  await tick();
  flushSync();
  await new Promise((r) => setTimeout(r, 0));
  flushSync();
  await new Promise((r) => setTimeout(r, 0));
  flushSync();
}

/** The blocked (or whole-plan paused) thread plan the server would project. */
function treeEnvelope(shape: PlanTreeShape, paused: boolean): OperatorEnvelope {
  const payload = envelope("t1", {
    done: 1,
    total: 3,
    liveness: paused ? "paused" : "blocked",
  });
  const entry = payload.plans[0];
  entry.plan.state = paused ? "paused" : "active";
  entry.display = paused
    ? planDisplay("waiting", "waiting")
    : planDisplay("needs_you", "human-review");
  entry.current_node_ids = shape.currentNodeIds;
  entry.next_node_ids = shape.nextNodeIds;
  entry.nodes = shape.nodes;
  return payload;
}

function treeRow(title: string): HTMLElement | undefined {
  return [...host.querySelectorAll<HTMLElement>(".tree-row")].find(
    (row) => row.querySelector(".row-title")?.textContent === title,
  );
}

function branchToggle(title: string): HTMLButtonElement | null {
  return treeRow(title)?.querySelector<HTMLButtonElement>(".branch-toggle") ?? null;
}

function topLevelTitles(): string[] {
  return [
    ...host.querySelectorAll('.tree-row[data-depth="0"] .row-title'),
  ].map((title) => title.textContent ?? "");
}

function buttonNamed(label: string): HTMLButtonElement | undefined {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent?.trim() === label,
  );
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  resetPlanRuntimeForTests();
  resetObserversForTests();
  token.set("test-bearer");
  connectionState.set("connected");
  wsListeners = new Map();
  vi.unstubAllGlobals();
});
afterEach(() => {
  if (head) unmount(head);
  head = null;
  if (component) unmount(component);
  component = null;
  host.remove();
  resetObserversForTests();
  vi.restoreAllMocks();
});

describe("ThreadPlanPane", () => {
  it("loads the thread operator projection and renders it through PlanTree (same renderer as scope cards)", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    expect(host.textContent).toContain("Loading");
    await settle();
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/api/plans/operator/threads/t1",
    );
    expect(host.querySelector(".plan-tree")).toBeTruthy();
    expect(host.textContent).toContain("Step for t1");
  });

  it("shows the calm empty state when the thread has no plan nodes yet", async () => {
    const empty = envelope("t1");
    empty.plans[0].nodes = [];
    mockFetch(200, empty, { ETag: '"r1"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    await settle();
    expect(host.textContent).toContain("No plan steps yet");
  });

  it("suppresses meaningless 0/0 progress metadata", async () => {
    const empty = envelope("t1", { done: 0, total: 0, open: 0, liveness: "idle" });
    empty.plans[0].nodes = [];
    mockFetch(200, empty, { ETag: '"r1"' });
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();
    expect(host.textContent).not.toContain("0/0");
  });

  /** Two finished steps the read does not list: the server counts them and keeps them in its history. */
  const TWO_FINISHED = [{ parent_id: null, done: 2, failed: 0, closed: 0, more: 2 }];

  it("an all-finished plan says nothing is open, shows no count, and folds its work (FW7)", async () => {
    const finished = envelope("t1", { done: 0, total: 0, open: 0, liveness: "idle" });
    finished.plans[0].current_node_ids = [];
    finished.plans[0].nodes = [];
    finished.plans[0].folds = TWO_FINISHED;
    finished.plans[0].root_count = 2;
    mockFetch(200, finished, { ETag: '"r1"' });
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();
    expect(host.textContent).toContain("Nothing open");
    expect(host.textContent).not.toContain("No plan steps yet");
    expect(host.querySelector(".fold-row")?.textContent).toContain("2 completed");
    expect(host.querySelector(".progress")).toBeNull();
    expect(treeRow("Finished a")).toBeUndefined();
  });

  it("a flat plan with finished work counts what is left, never 0 of N (#2190 review)", async () => {
    const flat = envelope("t1", { done: 0, total: 1, open: 1, liveness: "advancing" });
    flat.plans[0].folds = TWO_FINISHED;
    flat.plans[0].root_count = 3;
    mockFetch(200, flat, { ETag: '"r1"' });
    component = mountWithHead("t1");
    await settle();
    expect(host.querySelector(".fold-row")?.textContent).toContain("2 completed");
    expect(host.querySelector(".progress")?.textContent).toBe("1 left");
    expect(host.textContent).not.toContain("0/1");
  });

  it("a plan with open work shows no all-finished notice", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();
    expect(host.textContent).not.toContain("Nothing open");
  });

  it("pauses and resumes the whole plan with revision-safe mutations", async () => {
    let paused = false;
    const posted: unknown[] = [];
    let revision = 1;
    const fn = vi.fn(async (url: string, opts: RequestInit = {}) => {
      if (opts.method === "POST") {
        posted.push(JSON.parse(String(opts.body)));
        paused = !paused;
        revision += 1;
        return ok({
          plan: { id: "plan-t1", state: paused ? "paused" : "active" },
        });
      }
      const payload = envelope("t1");
      payload.plans[0].plan.state = paused ? "paused" : "active";
      payload.plans[0].plan.revision = revision;
      payload.plans[0].display = paused
        ? planDisplay("waiting", "waiting")
        : planDisplay("working", "active");
      payload.plans[0].nodes[0].execution.condition = paused ? "paused" : "running";
      payload.plans[0].nodes[0].display = paused
        ? { label_key: "waiting", tone_key: "waiting" }
        : { label_key: "working", tone_key: "active" };
      return ok(payload, paused ? '"paused"' : '"active"');
    });
    vi.stubGlobal("fetch", fn);
    component = mountWithHead("t1");
    await settle();

    const pause = [...host.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Pause",
    ) as HTMLButtonElement;
    expect(pause).toBeTruthy();
    pause.click();
    await settle();
    expect(posted[0]).toEqual({
      operation: "pause",
      mutation_id: "pause:plan-t1:1",
      params: { expected_revision: 1 },
    });
    expect(treeRow("Step for t1")?.textContent).toContain("Waiting");
    expect(
      [...host.querySelectorAll("button")].some(
        (button) => button.textContent?.trim() === "Resume",
      ),
    ).toBe(true);

    const resume = [...host.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Resume",
    ) as HTMLButtonElement;
    resume.click();
    await settle();
    expect(posted[1]).toEqual({
      operation: "resume",
      mutation_id: "resume:plan-t1:2",
      params: { expected_revision: 2 },
    });
    expect(treeRow("Step for t1")?.textContent).toContain("Working");
    expect(
      fn.mock.calls.filter(([, opts]) => opts?.method === "POST"),
    ).toHaveLength(2);
  });

  it("shows a refused task control's reason beside it, never at the top of the pane (#2431)", async () => {
    const fn = vi.fn(async (_url: string, opts: RequestInit = {}) => {
      if (opts.method === "POST") {
        return new Response(
          JSON.stringify({ detail: "The worker still has open steps" }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        );
      }
      const payload = envelope("t1");
      payload.plans[0].nodes[0].user_actions = ["mark_done", "close_node"];
      return ok(payload, '"mine"');
    });
    vi.stubGlobal("fetch", fn);
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();

    host.querySelector(".tree-row")?.dispatchEvent(new MouseEvent("contextmenu", {
      bubbles: true, clientX: 20, clientY: 20,
    }));
    await settle();
    const markDone = [...document.body.querySelectorAll("[role='menuitem']")].find(
      (button) => button.textContent?.trim() === "Mark done",
    ) as HTMLButtonElement;
    markDone.click();
    await settle();

    expect(document.body.querySelector("[role='menu'] [role='alert']")?.textContent?.trim())
      .toBe("The worker still has open steps");
    expect(host.querySelector(".lifecycle-error")).toBeNull();
    expect(host.textContent).not.toContain("This task changed");
  });

  it("keeps off-path branches and completed work reachable while the plan is blocked (PLANS-AC19)", async () => {
    mockFetch(200, treeEnvelope(blockedPlanTree(), false), { ETag: '"blocked"' });
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();

    expect(branchToggle("Build")?.getAttribute("aria-expanded")).toBe("true");
    expect(treeRow("Wait for API")).toBeTruthy();
    // Off-path branches stay in the tree, collapsed until the user opens them.
    expect(branchToggle("Docs")?.getAttribute("aria-expanded")).toBe("false");
    expect(branchToggle("Polish")?.getAttribute("aria-expanded")).toBe("false");
    expect(treeRow("Write guide")).toBeUndefined();
    expect(treeRow("Tune")).toBeUndefined();

    // Settled work is always visible; the fold row is the only control that
    // hides finished work, and it does not apply until a day has passed.
    expect(treeRow("Kickoff")).toBeTruthy();
    expect(buttonNamed("Hide completed")).toBeFalsy();

    branchToggle("Docs")!.click();
    await settle();
    expect(treeRow("Write guide")?.textContent).toContain("Working");
  });

  it("pausing a blocked plan reveals no row that was unreachable before and opens no branch", async () => {
    let paused = false;
    const fn = vi.fn(async (_url: string, opts: RequestInit = {}) => {
      if (opts.method === "POST") {
        paused = true;
        return ok({ plan: { id: "plan-t1", state: "paused" } });
      }
      return paused
        ? ok(treeEnvelope(pausedPlanTree(), true), '"paused"')
        : ok(treeEnvelope(blockedPlanTree(), false), '"blocked"');
    });
    vi.stubGlobal("fetch", fn);
    component = mountWithHead("t1");
    await settle();
    const before = topLevelTitles();

    buttonNamed("Pause")!.click();
    await settle();

    expect(topLevelTitles()).toEqual(before);
    expect(before).toEqual(["Build", "Docs", "Polish"]);
    expect(buttonNamed("Resume")).toBeTruthy();
    const toggles = [...host.querySelectorAll(".branch-toggle")];
    expect(toggles).toHaveLength(3);
    for (const toggle of toggles) {
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
    }
    expect(host.querySelector(".tree-row[aria-current]")).toBeNull();
  });

  it("does not offer a stale or offline plan mutation", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mountWithHead("t1");
    await settle();
    connectionState.set("disconnected");
    await settle();
    const pause = [...host.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Pause",
    ) as HTMLButtonElement;
    expect(pause.disabled).toBe(true);
  });

  it("unauthorized explains the inaccessible thread without exposing IDs", async () => {
    mockFetch(403, {});
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    await settle();
    expect(host.textContent).toContain("isn’t available");
    expect(host.textContent).not.toContain("t1");
  });

  it("names genuine human decisions and hands off to the canonical dock", async () => {
    const { __reset, focusRequest } =
      await import("$lib/stores/attentionDock.svelte");
    __reset();
    const payload = envelope("t1");
    payload.attention = { awaiting_count: 2 };
    routedFetch({
      "/api/plans/operator": () => ok(payload, '"r1"'),
      "/api/attention/queue": () => ok(queueBody(decisionItem(), 1)),
    });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();

    const indicator = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("decisions"),
    ) as HTMLButtonElement;
    expect(indicator).toBeTruthy();
    expect(indicator.textContent).toContain("2 decisions");
    expect(indicator.textContent).not.toContain("need you");
    expect(indicator.textContent).toContain("Open");
    // The thread plan never renders a decision body.
    expect(host.querySelector(".decision-chip")).toBeFalsy();
    expect(host.textContent).not.toContain("P5 needs one canonical home");

    let requests = 0;
    const stop = focusRequest.subscribe(() => {
      requests += 1;
    });
    indicator.click();
    await settle();
    stop();
    expect(requests).toBeGreaterThan(1);
    expect(host.querySelector(".decision-chip")).toBeFalsy();
  });

  it("clears a stale right-pane decision handoff instead of swallowing the click", async () => {
    const { __reset } = await import("$lib/stores/attentionDock.svelte");
    __reset();
    const payload = envelope("t1");
    payload.attention = { awaiting_count: 1 };
    const network = routedFetch({
      "/api/plans/operator": () => ok(payload, '"r1"'),
      "/api/attention/queue": () => ok(queueBody(null, 0)),
    });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();

    const indicator = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent?.includes("decision"),
    ) as HTMLButtonElement;
    indicator.click();
    await settle();

    expect(host.textContent).toContain("That item no longer needs you");
    expect(
      network.calls.filter((url) => url.includes("/api/attention/queue")),
    ).toHaveLength(1);
    expect(
      network.calls.filter((url) => url.includes("/api/plans/operator")).length,
    ).toBeGreaterThan(1);
    expect(host.querySelector(".decision-chip")).toBeFalsy();
  });

  it("leaves the plan state to its rows: no header sentence, no pause chrome", async () => {
    mockFetch(200, treeEnvelope(pausedPlanTree(), true));
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();

    expect(host.querySelector(".status-line")).toBeNull();
    expect(host.querySelector(".state-label")).toBeNull();
    expect(host.querySelector(".state-parts")).toBeNull();
    expect(buttonNamed("Pause")).toBeFalsy();
    expect(buttonNamed("Resume")).toBeFalsy();
  });

  it("shows no indicator when nothing is waiting on the user", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    expect(host.textContent).not.toContain("decision");
  });

  it("leaves the heading to its host, the section cap or the drawer", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mount(ThreadPlanPane, { target: host, props: { threadId: "t1" } });
    await settle();
    expect(host.querySelector("h2, h3")).toBeNull();
  });

  it("shows one count of unfinished work, with the read budget only in its hint", async () => {
    const large = envelope("t1");
    large.plans[0].progress.open = 12;
    large.plans[0].oversize = { open: 41, budget: 40 };
    mockFetch(200, large, { ETag: '"large"' });
    component = mountWithHead("t1");
    await settle();
    expect(host.querySelector(".plan-head-controls")?.textContent).not.toContain("41");
    const counts = host.querySelectorAll(".plan-head-controls .progress");
    expect(counts).toHaveLength(1);
    expect(counts[0].textContent?.trim()).toBe("12 left");
    expect(counts[0].classList.contains("oversize")).toBe(true);
    expect(counts[0].getAttribute("title")).toContain("40");
    unmount(component);
    unmount(head!);
    component = null;
    head = null;

    host.replaceChildren();
    resetPlanRuntimeForTests();
    resetObserversForTests();
    const small = envelope("t2");
    small.plans[0].oversize = null;
    mockFetch(200, small, { ETag: '"small"' });
    component = mountWithHead("t2");
    await settle();
    expect(host.querySelector(".plan-head-controls .progress")?.textContent).toBe("1 left");
    expect(host.querySelector(".oversize")).toBeNull();
    // jsdom applies no component CSS, so read the rule that keeps "3 left" on one line.
    expect(headSource).toMatch(/\.progress \{[^}]*white-space: nowrap/);
  });

  it("M5: renders next_node_ids as an explicit Next indicator, same as scope cards", async () => {
    const withNext = envelope("t1");
    withNext.plans[0].current_node_ids = [];
    withNext.plans[0].next_node_ids = ["step-1"];
    withNext.plans[0].nodes[0].state = "pending";
    withNext.plans[0].nodes[0].execution.condition = "ready";
    withNext.plans[0].nodes[0].display = { label_key: "working", tone_key: "active" };
    mockFetch(200, withNext, { ETag: '"r1"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    await settle();
    expect(host.querySelector(".state-icon")?.textContent).toBe("→");
  });

  it("loads independently per thread id (switching threads fetches the new thread)", async () => {
    mockFetch(200, envelope("t1"), { ETag: '"r1"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    await settle();
    expect(host.textContent).toContain("Step for t1");
    unmount(component);

    mockFetch(200, envelope("t2"), { ETag: '"r2"' });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t2" },
    });
    flushSync();
    await settle();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some((c) => c[0] === "/api/plans/operator/threads/t2"),
    ).toBe(true);
    expect(host.textContent).toContain("Step for t2");
  });

  it("issues no request on a timer, however long the pane stays open", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    const net = routedFetch({ "/threads/t1": () => ok(envelope("t1")) });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await vi.advanceTimersByTimeAsync(1);
    const before = net.calls.length;
    await vi.advanceTimersByTimeAsync(NO_POLL_WINDOW_MS);
    expect(net.calls.length).toBe(before);
    vi.useRealTimers();
  });

  it("does not refresh on visibility regain; a nudge or reconnect does", async () => {
    const net = routedFetch({ "/threads/t1": () => ok(envelope("t1")) });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    const before = net.calls.length;
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(net.calls.length).toBe(before);
  });

  it("recovers on a matching plans_changed nudge, refreshing silently (no stale flash) while online", async () => {
    let token2 = "r1";
    const net = routedFetch({
      "/threads/t1": () => ok(envelope("t1"), `"${token2}"`),
    });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    const before = net.calls.length;
    token2 = "r2";
    wsListeners.get("plans_changed")?.({
      type: "plans_changed",
      contract_version: 1,
      plans: [
        {
          plan_id: "x",
          revision: 2,
          owner: { kind: "thread", id: "t1" },
          scope: { workspace_id: "w", project_id: null },
        },
      ],
    });
    await settle();
    expect(net.calls.length).toBeGreaterThan(before);
    expect(host.textContent).not.toContain("showing last known data");
  });

  it("a child-owner nudge while expanded refreshes both the child and its host thread key", async () => {
    let hostDone = 1;
    let childTitle = "Child v1";
    const withSpawn = envelope("t1");
    withSpawn.plans[0].nodes = [
      operatorNode({
        id: "spawn",
        title: "Spawner",
        state: "active",
        child: {
          available: true,
          plan_id: "cp1",
          thread_id: "child-thread",
          progress: { done: 1, total: 2, open: 1, liveness: "advancing" },
        },
      }),
    ];
    const net = routedFetch({
      "/threads/t1": () => {
        const env = envelope("t1", { done: hostDone });
        env.plans[0].nodes = withSpawn.plans[0].nodes;
        return ok(env, `"h${hostDone}"`);
      },
      "/nodes/spawn/child": () =>
        ok(
          {
            contract_version: 2,
            as_of: "now",
            target: { kind: "thread", id: "child-thread", label: "Child" },
            revision_token: childTitle,
            attention: { awaiting_count: 0 },
            plans: [
              {
                plan: {
                  id: "cp1",
                  owner_kind: "thread",
                  owner_id: "child-thread",
                  state: "active",
                  revision: 1,
                  updated_at: "now",
                },
                owner: {
                  label: "Child",
                  project_id: null,
                  thread_id: "child-thread",
                },
                progress: { done: 1, total: 2, open: 1, liveness: "advancing", distribution: { ...EMPTY_DISTRIBUTION, active: 1 } },
                display: planDisplay("working", "active"),
                current_node_ids: [],
                next_node_ids: [],
                nodes: [operatorNode({ id: "c1", title: childTitle })],
                folds: [],
                root_count: 1,
              },
            ],
          },
          `"${childTitle}"`,
        ),
    });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    (host.querySelector(".spawn-chip") as HTMLButtonElement).click();
    await settle();
    expect(host.textContent).toContain("Child v1");
    const hostCallsBefore = net.calls.filter((u) =>
      u.includes("/threads/t1"),
    ).length;

    hostDone = 2;
    childTitle = "Child v2";
    wsListeners.get("plans_changed")?.({
      type: "plans_changed",
      contract_version: 1,
      plans: [
        {
          plan_id: "cp1",
          revision: 2,
          owner: { kind: "thread", id: "child-thread" },
          scope: { workspace_id: "w", project_id: null },
        },
      ],
    });
    await settle();

    expect(host.textContent).toContain("Child v2");
    expect(
      net.calls.filter((u) => u.includes("/threads/t1")).length,
    ).toBeGreaterThan(hostCallsBefore);
  });

  it("a nudge while the child is collapsed does not fetch the child (not observed)", async () => {
    const withSpawn = envelope("t1");
    withSpawn.plans[0].nodes = [
      operatorNode({
        id: "spawn",
        title: "Spawner",
        state: "active",
        child: {
          available: true,
          plan_id: "cp1",
          thread_id: "child-thread",
          progress: { done: 1, total: 2, open: 1, liveness: "advancing" },
        },
      }),
    ];
    const net = routedFetch({
      "/threads/t1": () => ok(withSpawn),
      "/nodes/spawn/child": () => ok(envelope("child-thread")),
    });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    wsListeners.get("plans_changed")?.({
      type: "plans_changed",
      contract_version: 1,
      plans: [
        {
          plan_id: "cp1",
          revision: 2,
          owner: { kind: "thread", id: "child-thread" },
          scope: { workspace_id: "w", project_id: null },
        },
      ],
    });
    await settle();
    expect(net.calls.filter((u) => u.includes("/child")).length).toBe(0);
  });

  it("hidden document: nothing fires on a timer", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    const net = routedFetch({ "/threads/t1": () => ok(envelope("t1")) });
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await vi.advanceTimersByTimeAsync(1);
    const before = net.calls.length;
    await vi.advanceTimersByTimeAsync(NO_POLL_WINDOW_MS * 2);
    expect(net.calls.length).toBe(before);
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    vi.useRealTimers();
  });

  it("retry issues exactly one request", async () => {
    let shouldFail = true;
    const fn = vi.fn(async () =>
      shouldFail
        ? {
            ok: false,
            status: 500,
            json: async (): Promise<unknown> => null,
            text: async () => "boom",
            headers: { get: () => null },
          }
        : ok(envelope("t1")),
    );
    vi.stubGlobal("fetch", fn);
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    flushSync();
    await settle();
    expect(fn.mock.calls.length).toBe(1);
    shouldFail = false;
    const retryButton = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Retry"),
    );
    retryButton?.click();
    await settle();
    expect(fn.mock.calls.length).toBe(2);
    expect(host.textContent).toContain("Step for t1");
  });
});

// ---------------------------------------------------------------------------
// F1/F2 coalescer remedy (wide-lens PASS verdict, Design A) — production-
// surface probes S1-S6, ported 1:1 from the verdict's own probes
// (zwl-surface.test.ts, describes S) into this file's existing conventions,
// per the implementation fence.
// ---------------------------------------------------------------------------

const REVISION_KEY = "revision" + "_token";

function spawnEnvelope(threadId: string): OperatorEnvelope {
  const env = envelope(threadId);
  env.plans[0].nodes = [operatorNode({
    id: "spawn",
    title: "Spawner",
    state: "active",
    child: {
      available: true,
      plan_id: "cp1",
      thread_id: "child-thread",
      progress: { done: 1, total: 2, open: 1, liveness: "advancing" },
    },
  })];
  return env;
}

function childEnvelope(title: string): OperatorEnvelope {
  return {
    contract_version: 2,
    as_of: "now",
    target: { kind: "thread", id: "child-thread", label: "Child" },
    [REVISION_KEY]: title,
    attention: { awaiting_count: 0 },
    plans: [
      {
        plan: {
          id: "cp1",
          owner_kind: "thread",
          owner_id: "child-thread",
          state: "active",
          revision: 1,
          updated_at: "now",
        },
        owner: { label: "Child", project_id: null, thread_id: "child-thread" },
        progress: { done: 1, total: 2, open: 1, liveness: "advancing", distribution: { ...EMPTY_DISTRIBUTION, active: 1 } },
        display: planDisplay("working", "active"),
        current_node_ids: [],
        next_node_ids: [],
        nodes: [operatorNode({ id: "c1", title })],
        folds: [],
        root_count: 1,
      },
    ],
  } as unknown as OperatorEnvelope;
}

interface DeferredCall {
  url: string;
  settle(value: unknown): void;
}

/** Records every request and lets each be settled individually, in any order. */
function deferredNetwork() {
  const calls: DeferredCall[] = [];
  const fn = vi.fn((url: string) => {
    let settleFn: (v: unknown) => void = () => {};
    const promise = new Promise((res) => {
      settleFn = res;
    });
    calls.push({ url, settle: settleFn });
    return promise;
  });
  vi.stubGlobal("fetch", fn);
  return {
    calls,
    countFor: (fragment: string) =>
      calls.filter((c) => c.url.includes(fragment)).length,
    settleAll: (fragment: string, body: unknown, etag = '"e"') => {
      for (const c of calls)
        if (c.url.includes(fragment)) c.settle(ok(body, etag));
    },
  };
}

describe("F1/F2 coalescer remedy: ordinary user paths that supersede an in-flight read", () => {
  it("S1 collapse then re-expand a subagent row on a slow network, then reconnect", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    net.settleAll("/threads/t1", spawnEnvelope("t1"));
    await settle();

    (host.querySelector(".spawn-chip") as HTMLButtonElement).click(); // expand -> child GET 1 (left in flight)
    await settle();
    (host.querySelector(".spawn-chip") as HTMLButtonElement).click(); // collapse -> child key retired
    await settle();
    (host.querySelector(".spawn-chip") as HTMLButtonElement).click(); // re-expand -> child GET 2 (the live one)
    await settle();

    net.calls
      .find((c) => c.url.includes("/child"))!
      .settle(ok(childEnvelope("Child v1")));
    await settle(); // the orphan lands

    connectionState.set("disconnected");
    connectionState.set("connected"); // reconnect -> refreshObserved()
    await settle();

    expect(net.countFor("/child")).toBe(2);
  });

  it("S2 (control) the same reconnect without the collapse race stays coalesced", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();
    net.settleAll("/threads/t1", spawnEnvelope("t1"));
    await settle();

    (host.querySelector(".spawn-chip") as HTMLButtonElement).click();
    await settle();
    connectionState.set("disconnected");
    connectionState.set("connected");
    await settle();

    expect(net.countFor("/child")).toBe(1);
  });

  it("S3 a token rotation landing mid-read, then a reconnect", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle(); // thread GET 1 in flight

    token.set("test-bearer-2"); // cancelAll() + refreshObserved()
    await settle(); // thread GET 2 issued

    net.calls[0].settle(ok(envelope("t1")));
    await settle(); // the orphan lands

    connectionState.set("disconnected");
    connectionState.set("connected");
    await settle();

    expect(net.countFor("/threads/t1")).toBe(2);
  });
});

describe("F1/F2 coalescer remedy: recovery of a surface whose read failed mid-body", () => {
  it("S4 a truncated response must not disable every later trigger", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();

    net.calls[0].settle({
      ok: true,
      status: 200,
      json: () =>
        Promise.reject(new TypeError("network error: body stream terminated")),
      headers: { get: () => '"partial"' },
    });
    await settle();

    connectionState.set("disconnected");
    connectionState.set("connected");
    await settle();
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    wsListeners.get("plans_changed")?.({
      type: "plans_changed",
      contract_version: 1,
      plans: [
        {
          plan_id: "p",
          revision: 2,
          owner: { kind: "thread", id: "t1" },
          scope: { workspace_id: "w", project_id: null },
        },
      ],
    });
    await settle();

    expect(net.countFor("/threads/t1")).toBeGreaterThan(1);
  });

  it("S5 a truncated response must leave a visible, retryable error, not a permanent skeleton", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();

    net.calls[0].settle({
      ok: true,
      status: 200,
      json: () =>
        Promise.reject(new TypeError("network error: body stream terminated")),
      headers: { get: () => '"partial"' },
    });
    await settle();

    expect(host.textContent).not.toContain("Loading");
    const retry = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Retry"),
    );
    expect(retry).toBeTruthy();
    retry!.click();
    await settle();
    expect(net.countFor("/threads/t1")).toBe(2);
  });

  it("S6 a 200 whose body is not an operator envelope must not wedge the key either", async () => {
    const net = deferredNetwork();
    component = mount(ThreadPlanPane, {
      target: host,
      props: { threadId: "t1" },
    });
    await settle();

    net.calls[0].settle(ok({ detail: "proxy says no" }));
    await settle();

    connectionState.set("disconnected");
    connectionState.set("connected");
    await settle();

    expect(net.countFor("/threads/t1")).toBeGreaterThan(1);
  });
});
