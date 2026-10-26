import { expect, test, type APIRequestContext, type Locator, type Page, type WebSocketRoute } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { createSession, openAppByUrl, TEST_TOKEN } from "../support/corsia";
import { setFocusMode } from "../support/focusMode";
import { setDebugVersionOverlay } from "../support/heldComments";
import { injectMessage } from "../support/mock";
import { operatorNode, planDisplay, withPlanContract } from "../../src/lib/__tests__/fixtures/operatorPlan";

/**
 * Disposable render harness for the person-step chip in the Focus history (corsia#687, ux-design
 * step 7, design only). The real shell runs on a disposable mock backend; only the thread's plan
 * read, its update cards, the plan mutation POST, the review bar's read and the server's
 * `plans_changed` push are fixtures. Frames go to CHIP_OUT; CHIP_ONLY filters by test name.
 */

const OUT = process.env.CHIP_OUT;
const ONLY = process.env.CHIP_ONLY ?? "";

const SIZES = [
  { w: 1440, h: 900 },
  { w: 393, h: 852 },
];
const THEMES = ["light", "dark"] as const;

type Step = { id: string; title: string; description: string; criteria: string };

const STEP_DOC: Step = {
  id: "step-signin",
  title: "Run the sign-in proof (about 5 minutes)",
  description: "",
  criteria: "The terminal prints PASS and the browser shows the test account signed in.",
};
const STEP_PLAIN: Step = {
  id: "step-restart",
  title: "Restart CORSIA so last night's two fixes go live",
  description:
    "Run this in a terminal on your Mac, then wait for it to print the new version.\n\n```\ncorsia restart\n```",
  criteria: "Running corsia status reports the new version.",
};
const STEP_LONG: Step = {
  id: "step-longtitle",
  title:
    "Restart CORSIA on the test Mac, wait for the new version to print, then sign in with the disposable account and confirm the Sidebar loads",
  description: "Run this in a terminal on your Mac.\n\n```\ncorsia restart\n```",
  criteria: "corsia status reports the new version and the Sidebar loads.",
};
const SEVEN: Step[] = [
  "Run the sign-in proof",
  "Restart CORSIA on the test Mac",
  "Rotate the staging API key",
  "Approve the Apple notarization prompt",
  "Plug in the test iPhone and trust this Mac",
  "Run the backup restore drill",
  "Sign the release manifest",
].map((title, i) => ({
  id: `step-seven-${i}`,
  title,
  description: `Do this on your Mac, then come back.\n\n\`\`\`\nscripts/step-${i} --confirm\n\`\`\``,
  criteria: "The script prints OK.",
}));

const DOC_TEXT = [
  "# Run the sign-in proof",
  "Sign in on the disposable instance and run the proof script, so the agent can confirm the new sign-in path end to end.",
  "```\nscripts/signin-proof --fresh\n```",
  "It opens a browser window. Sign in with the test account and close the window when the terminal prints PASS.",
  "Then tap Done below. If you can't run it, tap Won't do and the agent will follow up in chat.",
].join("\n\n");

type Outcome = "done" | "closed";
type World = {
  steps: Step[];
  /** Plan state of each step: absent means still waiting on the person. */
  settled: Record<string, Outcome>;
  doc: { id: string; path: string; name: string } | null;
  docStepId: string | null;
  revision: number;
};

const SETTLED_AT = () => new Date().toISOString();

function stepNode(world: World, step: Step) {
  const outcome = world.settled[step.id];
  const at = outcome ? SETTLED_AT() : "2026-10-26T18:00:00Z";
  const artifacts =
    world.doc && world.docStepId === step.id
      ? [
          {
            id: world.doc.id,
            role: "input",
            integration: "corsia",
            type: "document",
            external_id: world.doc.path,
            title: "Run the sign-in proof: how to",
            path: world.doc.path,
            state: "active",
            attention_state: null,
          },
        ]
      : [];
  return operatorNode({
    id: step.id,
    title: step.title,
    description: step.description,
    acceptance_criteria: step.criteria,
    assignee: "user",
    state: outcome === "done" ? "accepted" : outcome === "closed" ? "closed" : "pending",
    requires_user_action: !outcome,
    display: outcome
      ? { label_key: "finished", tone_key: outcome === "closed" ? "closed" : "complete" }
      : { label_key: "needs_you", tone_key: "human-review" },
    user_actions: outcome ? [] : ["mark_done", "close_node"],
    artifacts,
    revision: 1,
    updated_at: at,
    last_transition_at: at,
  } as never);
}

function envelope(threadId: string, world: World) {
  const nodes = world.steps.map((step) => stepNode(world, step));
  const open = world.steps.filter((s) => !world.settled[s.id]);
  return {
    contract_version: 2,
    as_of: SETTLED_AT(),
    target: { kind: "thread", id: threadId, label: "Sign-in proof" },
    revision_token: `chip-${threadId}-${world.revision}`,
    attention: { awaiting_count: 0 },
    plans: [
      withPlanContract({
        plan: {
          id: `plan-${threadId}`,
          owner_kind: "thread",
          owner_id: threadId,
          state: "active",
          revision: world.revision,
          updated_at: SETTLED_AT(),
        },
        owner: { label: "Sign-in proof", project_id: null, thread_id: threadId },
        progress: {
          done: world.steps.length - open.length,
          total: nodes.length,
          open: open.length,
          liveness: "waiting",
        },
        display: planDisplay("needs_you", "human-review"),
        current_node_ids: [],
        next_node_ids: open.map((s) => s.id),
        nodes,
      } as never),
    ],
  };
}

type Turn = { lead: string; text?: string; step?: Step; working?: number };
type Scene = {
  steps: Step[];
  turns: Turn[];
  docStepId?: string;
  reviewTile?: boolean;
};

const ONE_TURN = (step: Step): Turn[] => [
  {
    lead: "I checked the proof requirements.",
    text: "The sign-in proof needs you at a terminal. I added a step for it; I'll carry on with the write-up until you tell me it passed.",
    step,
  },
  { lead: "Drafting the verification write-up.", text: "The write-up is drafted. It is waiting on the proof result.", working: 2 },
];

const SCENES: Record<string, Scene> = {
  "one-doc": { steps: [STEP_DOC], turns: ONE_TURN(STEP_DOC), docStepId: STEP_DOC.id },
  "one-plain": { steps: [STEP_PLAIN], turns: ONE_TURN(STEP_PLAIN) },
  "long-title": { steps: [STEP_LONG], turns: ONE_TURN(STEP_LONG) },
  // The steps were raised through working lines with one shown message after them: one Focus stretch.
  seven: {
    steps: SEVEN,
    turns: [
      ...SEVEN.map((step, i) => ({ lead: `Working through item ${i + 1} of the release checklist.`, step, working: 1 })),
      { lead: "Summarising.", text: "Seven things are waiting on you; I'll carry on with the rest until they're done." },
    ],
  },
  neighbour: {
    steps: [STEP_DOC],
    docStepId: STEP_DOC.id,
    reviewTile: true,
    turns: [
      { lead: "Reading the proof requirements.", text: "I drafted the sign-in proof plan. It needs your review before I run anything." },
      { lead: "Checking the test account.", text: "The proof itself needs you at a terminal. I added a step for it.", step: STEP_DOC },
    ],
  },
};

async function seedMessages(request: APIRequestContext, threadId: string, scene: Scene) {
  const minute = new Date();
  minute.setSeconds(0, 0);
  let seq = 0;
  const seqOf: Record<string, number> = {};
  const add = async (role: string, content: string, metadata: Record<string, unknown> = {}) => {
    await injectMessage(request, threadId, { role, content, metadata }, TEST_TOKEN);
    seq += 1;
    return seq;
  };
  await add("user", "Please prepare the sign-in proof.");
  for (const turn of scene.turns) {
    const at = await add("assistant", turn.lead, {
      tool_calls: Array.from({ length: turn.working ?? 1 }, (_, i) => ({ id: `call-${seq}-${i}`, name: "read_file", arguments: {} })),
    });
    if (turn.step) seqOf[turn.step.id] = at;
    if (turn.text) {
      const shown = await add("assistant", turn.text, { for_user: true });
      if (turn.step) seqOf[turn.step.id] = shown;
    }
  }
  return { seqOf, createdAt: minute.toISOString() };
}

async function seedDocument(request: APIRequestContext, threadId: string, label: string) {
  const roots = await request.get("/api/files/tree", { params: { token: TEST_TOKEN } });
  const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === "workspace").path as string;
  const name = `${label}-${Date.now()}.md`;
  const docPath = path.join(workspace, name);
  const put = await request.put("/api/files/content", {
    params: { token: TEST_TOKEN },
    data: { path: docPath, text: DOC_TEXT, thread_id: threadId, create: true },
  });
  expect(put.ok(), await put.text()).toBeTruthy();
  return { id: (await put.json()).artifact.id as string, path: docPath, name };
}

/** The app's live socket, passed through; `push` sends the server's plans_changed nudge for the thread. */
async function routeSocket(page: Page, threadId: string, world: World) {
  let socket: WebSocketRoute | null = null;
  await page.routeWebSocket(
    (url) => url.pathname === "/ws",
    (client) => {
      const server = client.connectToServer();
      socket = client;
      client.onMessage((payload) => server.send(payload));
      server.onMessage((payload) => client.send(payload));
    },
  );
  return {
    push() {
      if (!socket) throw new Error("The app's WebSocket was not captured");
      socket.send(
        JSON.stringify({
          type: "plans_changed",
          contract_version: 2,
          plans: [
            {
              plan_id: `plan-${threadId}`,
              revision: world.revision,
              owner: { kind: "thread", id: threadId },
              scope: { workspace_id: null, project_id: null },
            },
          ],
        }),
      );
    },
    async connected() {
      await expect.poll(() => socket !== null, { timeout: 15_000 }).toBe(true);
    },
  };
}

async function shot(page: Page, name: string, park = true) {
  if (!OUT) return;
  fs.mkdirSync(OUT, { recursive: true });
  if (park) await page.mouse.move(2, 2);
  await page.waitForTimeout(450);
  await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: false });
  const check = await page.evaluate(() => {
    const over = document.documentElement.scrollWidth - window.innerWidth;
    const rows = [...document.querySelectorAll<HTMLElement>("[data-person-step]")];
    const describe = (row: HTMLElement) => {
      const face = row.querySelector<HTMLElement>("[data-step-chip], .ev-chip");
      const r = face?.getBoundingClientRect();
      const title = face?.querySelector<HTMLElement>(".title, .ev-summary");
      const clipped = title ? title.scrollWidth > title.clientWidth + 1 && getComputedStyle(title).textOverflow !== "ellipsis" : false;
      return `${row.dataset.stepState}x${row.dataset.stepCount}:h${Math.round(r?.height ?? 0)}w${Math.round(r?.width ?? 0)}${clipped ? ":CLIPPED" : ""}`;
    };
    return { over, chips: rows.map(describe) };
  });
  fs.appendFileSync(path.join(OUT, "_checks.txt"), `${name} overflow=${check.over} rows=${JSON.stringify(check.chips)}\n`);
}

/** Reverse-tab from the composer until the chip's Do → button holds keyboard focus-visible. */
async function tabTo(page: Page, target: Locator) {
  await page.getByRole("textbox", { name: "Message CORSIA" }).click();
  for (let i = 0; i < 20; i += 1) {
    await page.keyboard.press("Shift+Tab");
    if (await target.evaluate((el) => document.activeElement === el && el.matches(":focus-visible"))) return;
  }
  throw new Error("Keyboard Tab never reached the Do → button with :focus-visible.");
}

async function assertInert(page: Page, row: Locator, settled: boolean) {
  const layers = row.locator(".slot-layer");
  const inactive = settled ? layers.first() : layers.nth(1);
  await expect(inactive).toHaveAttribute("inert", "");
  await expect(inactive).toHaveAttribute("aria-hidden", "true");
  if (settled) {
    const hidden = row.locator("[data-step-chip]");
    await page.getByRole("textbox", { name: "Message CORSIA" }).click();
    for (let i = 0; i < 20; i += 1) {
      await page.keyboard.press("Shift+Tab");
      await expect(hidden).not.toBeFocused();
    }
  }
}

/** Sample the one-step slot while it changes: the row never collapses, and a face is always visible. */
function sampleSwap(page: Page) {
  return page.evaluate(async () => {
    const frames: Array<{ state: string; slot: number; visible: boolean; faces: number[] }> = [];
    for (let i = 0; i < 30; i += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const row = document.querySelector<HTMLElement>("[data-person-step]");
      const layers = [...(row?.querySelectorAll<HTMLElement>(".slot-layer") ?? [])];
      frames.push({
        state: row?.dataset.stepState ?? "missing",
        slot: row?.querySelector<HTMLElement>(".slot")?.getBoundingClientRect().height ?? 0,
        visible: layers.some((l) => l.getBoundingClientRect().height > 0 && Number(getComputedStyle(l).opacity) > 0.01),
        faces: layers.map((l) => l.getBoundingClientRect().height),
      });
    }
    return frames;
  });
}

function assertNoJump(samples: Awaited<ReturnType<typeof sampleSwap>>) {
  expect(samples.some((s) => s.state !== "open")).toBe(true);
  expect(samples.every((s) => s.visible)).toBe(true);
  expect(samples.every((s) => s.slot > 0)).toBe(true);
  for (const s of samples) {
    const smallest = Math.min(...s.faces.filter((h) => h > 0));
    expect(s.slot).toBeGreaterThanOrEqual(smallest - 1);
  }
  if (OUT) fs.appendFileSync(path.join(OUT, "_checks.txt"), `swap slot heights=${JSON.stringify([...new Set(samples.map((s) => Math.round(s.slot)))])}\n`);
}

async function openThread(page: Page, title: string, phone: boolean) {
  await openAppByUrl(page);
  if (phone) await page.getByRole("button", { name: "Toggle sidebar" }).click();
  await page.getByText(title).first().click();
  await expect(page.getByRole("textbox", { name: "Message CORSIA" })).toBeVisible({ timeout: 20_000 });
}

const CASES: Array<{ id: string; scene: keyof typeof SCENES; focus: boolean[] }> = [
  { id: "one-doc", scene: "one-doc", focus: [true, false] },
  { id: "one-plain", scene: "one-plain", focus: [true] },
  { id: "seven", scene: "seven", focus: [true, false] },
  { id: "long-title", scene: "long-title", focus: [true] },
  { id: "focus-ring", scene: "one-doc", focus: [true] },
  { id: "do-document", scene: "one-doc", focus: [true] },
  { id: "done", scene: "one-doc", focus: [true] },
  { id: "wontdo", scene: "one-doc", focus: [true] },
  { id: "do-details", scene: "one-plain", focus: [true] },
  { id: "group-do", scene: "seven", focus: [true] },
  { id: "group-settle", scene: "seven", focus: [true] },
  { id: "neighbour", scene: "neighbour", focus: [true] },
  { id: "reduced-motion", scene: "one-doc", focus: [true] },
];

for (const size of SIZES) {
  for (const theme of THEMES) {
    for (const { id, scene: sceneName, focus } of CASES) {
      for (const focusOn of focus) {
        // Reduced motion needs only one theme per width.
        if (id === "reduced-motion" && theme === "dark") continue;
        const tag = `${size.w}-${theme}-focus-${focusOn ? "on" : "off"}`;
        const name = `${id} ${tag}`;
        if (ONLY && !name.includes(ONLY)) continue;
        test(`chip ${name}`, async ({ page, request }) => {
          const phone = size.w <= 768;
          await page.setViewportSize({ width: size.w, height: size.h });
          await page.addInitScript((t) => localStorage.setItem("corsia_theme", t), theme);
          if (id === "reduced-motion") await page.emulateMedia({ reducedMotion: "reduce" });
          await setFocusMode(request, focusOn);
          await setDebugVersionOverlay(request, true);

          const scene = SCENES[sceneName];
          const title = `chip-${id}-${Date.now()}`;
          const threadId = await createSession(request, title);
          const seeded = await seedMessages(request, threadId, scene);
          const doc = scene.docStepId ? await seedDocument(request, threadId, "signin-proof") : null;
          const world: World = { steps: scene.steps, settled: {}, doc, docStepId: scene.docStepId ?? null, revision: 1 };

          const settle = (nodeId: string, outcome: Outcome) => {
            world.settled[nodeId] = outcome;
            world.revision += 1;
          };

          await page.route("**/api/threads/*/updates*", (route) => {
            const cards = scene.turns.flatMap((turn, i) => {
              const step = turn.step;
              if (!step) return [];
              const outcome = world.settled[step.id];
              return [
                {
                  id: i + 1,
                  thread_id: threadId,
                  kind: "person_step",
                  node_id: step.id,
                  title: step.title,
                  after_seq: seeded.seqOf[step.id],
                  created_at: seeded.createdAt,
                  node_exists: true,
                  asks_user: true,
                  blocked_asks_user: false,
                  step_state: outcome ?? "open",
                },
              ];
            });
            return route.fulfill({
              status: 200,
              contentType: "application/json",
              body: JSON.stringify({ cards, has_more: false, divider: null }),
            });
          });
          await page.route(`**/api/plans/operator/threads/${threadId}`, (route) =>
            route.fulfill({
              status: 200,
              contentType: "application/json",
              headers: { ETag: `"chip-${world.revision}"` },
              body: JSON.stringify(envelope(threadId, world)),
            }),
          );
          await page.route("**/api/plans/operator/thread-states*", async (route) => {
            try {
              const res = await route.fetch();
              const body = (await res.json()) as { states?: Record<string, unknown> };
              const owed = world.steps.filter((s) => !world.settled[s.id]).map((s) => ({ kind: "hold", title: s.title, node_id: s.id }));
              const states = { ...(body.states ?? {}) };
              states[threadId] = {
                ...((states[threadId] as object) ?? {}),
                state: owed.length ? "needs_input" : "moving",
                label_key: null,
                paused: false,
                attention_items: owed,
                has_update: false,
              };
              await route.fulfill({ response: res, json: { ...body, states } });
            } catch (error) {
              if (page.isClosed()) return;
              throw error;
            }
          });
          if (doc) {
            await page.route(`**/api/attention/${doc.id}/review`, (route) => {
              const step = scene.steps.find((s) => s.id === scene.docStepId)!;
              return route.fulfill({
                status: 200,
                contentType: "application/json",
                body: JSON.stringify({
                  ask_raised: false,
                  hold_ids: [],
                  ask_reason: null,
                  ask_raised_at: null,
                  asker: null,
                  other_askers: [],
                  other_asks: [],
                  other_request_count: 0,
                  round_feedback_count: 0,
                  round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
                  person_step: {
                    node_id: step.id,
                    node_title: step.title,
                    node_revision: 1,
                    plan_id: `plan-${threadId}`,
                    owner_thread_id: threadId,
                    owner_name: "Sign-in proof",
                  },
                }),
              });
            });
          }
          if (scene.reviewTile) {
            await page.route("**/api/threads/*/messages*", async (route) => {
              const res = await route.fetch();
              const body = await res.json();
              for (const m of body?.messages ?? []) {
                if (m.role === "assistant" && String(m.content).startsWith("I drafted the sign-in proof plan")) {
                  m.cards = [
                    {
                      kind: "ask",
                      artifact_id: "artifact-proof-plan",
                      title: "Sign-in proof plan",
                      type: "markdown",
                      icon: "file-text",
                      name: "sign-in-proof-plan.md",
                      path: "shared/planning/sign-in-proof-plan.md",
                      available: true,
                      opens: true,
                      hold_id: "hold-proof-plan",
                      state: "needs_review",
                    },
                  ];
                }
              }
              await route.fulfill({ response: res, json: body });
            });
          }
          const socket = await routeSocket(page, threadId, world);
          const mutated: Array<{ nodeId: string; operation: string }> = [];
          await page.route("**/api/plans/*/mutations", async (route) => {
            const body = route.request().postDataJSON();
            const nodeId = String(body?.params?.node_id ?? "");
            mutated.push({ nodeId, operation: String(body?.operation) });
            settle(nodeId, body?.operation === "close_node" ? "closed" : "done");
            await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
            socket.push();
          });

          await openThread(page, title, phone);
          await socket.connected();
          const rows = page.locator("[data-person-step]");
          await expect(rows.first()).toBeVisible({ timeout: 20_000 });
          const row = (step: Step) => page.locator(`[data-person-step="${step.id}"]`);
          const chip = (step: Step) => row(step).locator("[data-step-chip]");
          const first = scene.steps[0];

          if (id === "one-doc" || id === "one-plain" || id === "long-title") {
            await expect(chip(first)).toBeVisible();
            await expect(chip(first)).toContainText("Do →");
            await expect(chip(first)).toContainText("Ready for you");
            await shot(page, `${id}-focus-${focusOn ? "on" : "off"}-${size.w}-${theme}`);
          }

          if (id === "seven") {
            if (focusOn) {
              await expect(rows).toHaveCount(1);
              await expect(rows.first()).toContainText("7 steps");
              await expect(rows.first()).toContainText("Do →");
            } else {
              await expect(rows).toHaveCount(7);
            }
            await shot(page, `seven-focus-${focusOn ? "on" : "off"}-${size.w}-${theme}`);
          }

          if (id === "focus-ring") {
            await tabTo(page, chip(first));
            const ring = await chip(first).evaluate((el) => {
              const s = getComputedStyle(el);
              return { outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth, shadow: s.boxShadow };
            });
            if (OUT) fs.appendFileSync(path.join(OUT, "_checks.txt"), `focus-ring ${tag} ${JSON.stringify(ring)}\n`);
            expect(ring.outlineStyle !== "none" || ring.shadow !== "none").toBe(true);
            await shot(page, `focus-ring-${size.w}-${theme}`, false);
          }

          if (id === "do-document") {
            await chip(first).click();
            await expect(page.getByRole("button", { name: "Done", exact: true })).toBeVisible({ timeout: 15_000 });
            await expect(page.getByRole("button", { name: "Won’t do", exact: true })).toBeVisible();
            await shot(page, `do-document-${size.w}-${theme}`);
          }

          if (id === "done" || id === "wontdo" || id === "reduced-motion") {
            await chip(first).click();
            const action = id === "wontdo" ? "Won’t do" : "Done";
            await expect(page.getByRole("button", { name: action, exact: true })).toBeVisible({ timeout: 15_000 });
            const swap = sampleSwap(page);
            await page.getByRole("button", { name: action, exact: true }).click();
            await expect(row(first)).toHaveAttribute("data-step-state", id === "wontdo" ? "closed" : "done", { timeout: 15_000 });
            const samples = await swap;
            assertNoJump(samples);
            if (id === "reduced-motion") {
              // Reduced motion: the faces swap on one frame, never half-faded.
              const mid = samples.filter((s) => s.state !== "open").slice(0, 3);
              if (OUT) fs.appendFileSync(path.join(OUT, "_checks.txt"), `reduced-motion first settled frames=${JSON.stringify(mid)}\n`);
            }
            expect(mutated.map((m) => m.operation)).toEqual([id === "wontdo" ? "close_node" : "mark_done"]);
            await expect(row(first).locator(".ev-chip .ev-label")).toHaveText(id === "wontdo" ? "Won’t do" : "Done");
            await expect(row(first).locator(".ev-chip .ev-summary")).toHaveText(first.title);
            await assertInert(page, row(first), true);
            // The document stays open on a phone; close it so the receipt in the chat is the frame.
            if (phone) {
              const back = page.getByRole("button", { name: /^(Back|Close)/ }).first();
              if (await back.isVisible()) await back.click();
            }
            await shot(page, `${id}-${size.w}-${theme}`);
          }

          if (id === "do-details") {
            await chip(first).click();
            if (phone) await expect(page.getByRole("dialog", { name: /^Plan —/ })).toBeVisible({ timeout: 15_000 });
            await expect(page.getByRole("region", { name: `${first.title} details` })).toBeVisible({ timeout: 15_000 });
            await shot(page, `do-details-${size.w}-${theme}`);
          }

          if (id === "group-do") {
            await rows.first().locator("[data-step-chip]").click();
            if (phone) await expect(page.getByRole("dialog", { name: /^Plan —/ })).toBeVisible({ timeout: 15_000 });
            // A group's Do → shows the plan at the first waiting step; each step opens by its own row.
            const rowBody = page.getByRole("listitem").filter({ hasText: SEVEN[0].title }).locator(".row-body").first();
            await expect(rowBody).toBeFocused({ timeout: 15_000 });
            await shot(page, `group-do-${size.w}-${theme}`);
          }

          if (id === "group-settle") {
            await expect(rows.first()).toContainText("7 steps");
            // The first step is settled somewhere else (the document bar or the plan row); the push follows.
            settle(SEVEN[0].id, "done");
            socket.push();
            await expect(page.locator(`[data-person-step]`).first()).toBeVisible();
            await expect(page.getByText("6 steps")).toBeVisible({ timeout: 15_000 });
            await expect(page.locator(".ev-chip").filter({ hasText: SEVEN[0].title })).toBeVisible();
            await shot(page, `group-settle-${size.w}-${theme}`);
          }

          if (id === "neighbour") {
            await expect(chip(first)).toBeVisible();
            await expect(page.getByText("Needs your review").first()).toBeVisible({ timeout: 15_000 });
            await shot(page, `neighbour-${size.w}-${theme}`);
          }
        });
      }
    }
  }
}
