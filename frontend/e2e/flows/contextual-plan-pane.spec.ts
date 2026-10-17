import { expect, test } from "@playwright/test";
import {
  createSession,
  openAppByUrl,
  saveEvidenceScreenshot,
} from "../support/corsia";
import {
  EMPTY_DISTRIBUTION,
  operatorNode,
  planDisplay,
  withPlanContract,
} from "../../src/lib/__tests__/fixtures/operatorPlan";

function node(overrides: Parameters<typeof operatorNode>[0] = {}) {
  return operatorNode({
    title: "Plan step",
    acceptance_criteria: "Done when verified",
    updated_at: "2026-08-13T00:00:00Z",
    last_transition_at: "2026-08-13T00:00:00Z",
    ...overrides,
  });
}

function envelope(
  threadId: string,
  nodes: ReturnType<typeof node>[],
  options: {
    current?: string[];
    next?: string[];
    done?: number;
    total?: number;
  } = {},
) {
  const done = options.done ?? 1;
  // The contract fields the decoder requires (oversize, folds, root_count) come from the shared fixture helper.
  const entry = withPlanContract({
    plan: {
      id: `plan-${threadId}`,
      owner_kind: "thread",
      owner_id: threadId,
      state: "active",
      revision: 1,
      updated_at: "2026-08-13T00:00:00Z",
    },
    owner: { label: "Conversation", project_id: null, thread_id: threadId },
    progress: {
      done,
      total: options.total ?? nodes.length,
      open: nodes.length - done,
      liveness: "advancing",
    },
    display: planDisplay("working", "active"),
    current_node_ids: options.current ?? [],
    next_node_ids: options.next ?? [],
    nodes,
  });
  return {
    contract_version: 2,
    as_of: "2026-08-13T00:00:00Z",
    target: { kind: "thread", id: threadId, label: "Conversation" },
    revision_token: `revision-${threadId}`,
    attention: { awaiting_count: 0 },
    plans: [
      {
        ...entry,
        progress: {
          ...entry.progress,
          distribution: {
            ...EMPTY_DISTRIBUTION,
            done,
            active: options.current?.length ?? 0,
            pending_ready: options.next?.length ?? 0,
          },
        },
      },
    ],
  };
}

test.describe("@flow:contextual-plan @surface:chat @surface:plans contextual thread plan", () => {
  test("shows task details as independent labelled facts", async ({ page, request }, testInfo) => {
    if (testInfo.project.name === "chromium-mobile") {
      await page.setViewportSize({ width: 390, height: 844 });
    }
    const threadTitle = `task-details-${Date.now()}`;
    const threadId = await createSession(request, threadTitle);
    const taskTitle = "Inspect task details";
    const turns = { completed: 3, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
    const amount = {
      tokens: { known: 1_800_000, unknown_rows: 56 },
      model_cost_usd: { known: null, unknown_rows: 56 },
      usage_recording_failures: 0,
      execution: { seconds: 392, unknown_turns: 0, turns },
      waiting: { seconds: 10_320 },
    };
    const plan = envelope(
      threadId,
      [node({
        id: "details",
        title: taskTitle,
        state: "active",
        user_actions: ["mark_done", "pause_node", "close_node"],
        node_cost: { own: amount, rolled: amount },
      })],
      { current: ["details"], done: 0, total: 1 },
    );

    await page.route(`**/api/plans/operator/threads/${threadId}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: '"task-details"' },
        body: JSON.stringify(plan),
      }),
    );

    await openAppByUrl(page);
    const isMobile = (page.viewportSize()?.width ?? 0) <= 768;
    if (isMobile) {
      await page.getByRole("button", { name: "Toggle sidebar" }).click();
    }
    await page.getByText(threadTitle).first().click();

    if (isMobile) {
      const progressChip = page.getByRole("button", { name: /^Open plan —/ });
      await expect(progressChip).toBeVisible();
      await progressChip.click();
      await expect(page.getByRole("dialog", { name: /^Plan —/ })).toBeVisible();
    } else {
      const planSection = page.getByRole("button", { name: "Plan", exact: true });
      const inView = async () => {
        const box = await planSection.boundingBox();
        const width = page.viewportSize()?.width ?? 0;
        return !!box && box.x >= 0 && box.x + box.width <= width;
      };
      await expect(planSection).toBeAttached();
      if (!(await inView())) {
        await page.getByRole("button", { name: "Toggle panel" }).click();
      }
      await expect.poll(inView).toBe(true);
    }

    const row = page.getByRole("listitem").filter({ hasText: taskTitle }).first();
    await row.locator(".row-body").click();
    const details = page.getByRole("region", { name: `${taskTitle} details` });
    await expect(details.locator(".status")).toHaveText("Status: Working");
    await expect(details.locator(".working")).toHaveText("Working: 6m 32s");
    await expect(details.locator(".waiting")).toHaveText("Waiting: 2h 52m");
    await expect(details.locator(".tokens")).toHaveText("Tokens: at least 1.8M");
    await expect(details.locator(".cost")).toHaveCount(0);
    await expect(details.locator("details, button.reveal, button")).toHaveCount(0);
    await expect(details).not.toContainText(/unknown|unpriced|not reported/i);
    await saveEvidenceScreenshot(
      page,
      testInfo,
      isMobile ? "task-details-390px" : "task-details-desktop",
      { fullPage: false },
    );

    if (isMobile) {
      await row.dispatchEvent("pointerdown", {
        pointerType: "touch",
        clientX: 120,
        clientY: 300,
      });
      await page.waitForTimeout(510);
      const menu = page.getByRole("menu", { name: `Actions for ${taskTitle}` });
      await expect(menu).toBeVisible();
      // The menu scales in; judge the targets once the entrance settles.
      for (const item of await menu.getByRole("menuitem").all()) {
        await expect
          .poll(async () => (await item.boundingBox())?.height ?? 0)
          .toBeGreaterThanOrEqual(44);
      }
      await saveEvidenceScreenshot(page, testInfo, "task-actions-390px", { fullPage: false });
      await row.dispatchEvent("pointerup", { pointerType: "touch" });
    }
  });

  test("keeps Plans contextual and opens a canonical subagent conversation in the artifact leaf", async ({
    page,
    request,
  }, testInfo) => {
    const stamp = Date.now();
    const parentTitle = `parent-plan-${stamp}`;
    const childTitle = `child-plan-${stamp}`;
    const parentId = await createSession(request, parentTitle);
    const childId = await createSession(request, childTitle);
    const delegatedTitle = "Delegate projection audit";

    const parentNodes = [
      node({ id: "done", title: "Map current behavior", state: "accepted" }),
      node({
        id: "delegated",
        title: delegatedTitle,
        state: "active",
        child: {
          available: true,
          plan_id: `plan-${childId}`,
          thread_id: childId,
          progress: { done: 0, total: 1, open: 1, liveness: "advancing" },
        },
        discussion_artifact_id: "legacy-discussion-1",
      }),
      // The server stamps the next node's display; this read is fulfilled by
      // the flow, so the fixture carries the server's answer.
      node({
        id: "next",
        title: "Wire the right pane",
        state: "pending",
        display: { label_key: "working", tone_key: "active" },
      }),
      node({ id: "todo", title: "Verify responsive layout", state: "pending" }),
      node({
        id: "blocked",
        title: "Await required fixture",
        state: "pending",
        description: "Fixture owner has not published it yet.",
        execution: {
          condition: "waiting",
          primary_reason: "Fixture owner has not published it yet.",
          causes: [{
            kind: "waiting",
            reason_key: "external_fixture",
            reason: "Fixture owner has not published it yet.",
            owner_kind: "external",
            owner_id: null,
          }],
          selection: null,
          conflict: null,
          attempt_count: 0,
          friction_count: 0,
          attempts: [],
        },
      }),
      node({
        id: "review",
        title: "Parent harvest",
        state: "review_requested",
        review: {
          kind: "parent_agent",
          label: "Awaiting parent agent",
          actor: { kind: "thread", id: "parent-thread", label: "Parent agent" },
        },
      }),
    ];
    const parentPlan = envelope(parentId, parentNodes, {
      current: ["delegated"],
      next: ["next"],
      done: 1,
      total: 6,
    });
    const childPlan = envelope(
      childId,
      [
        node({
          id: "child-active",
          title: "Inspect projection contract",
          state: "active",
        }),
      ],
      { current: ["child-active"], done: 0, total: 1 },
    );
    const childMessages = [
      {
        id: "child-message-1",
		thread_id: childId,
        role: "assistant",
        content: "Child is inspecting the projection contract.",
        created_at: "2026-08-13T00:00:01Z",
        // Focus shows only what the agent said to the person; every other assistant row is work.
        metadata: { for_user: true },
      },
    ];

    await page.route(`**/api/plans/operator/threads/${parentId}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: '"parent-plan"' },
        body: JSON.stringify(parentPlan),
      }),
    );
    await page.route("**/api/plans/operator/nodes/delegated/child", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: '"child-plan"' },
        body: JSON.stringify(childPlan),
      }),
    );
    await page.route(`**/api/threads/${childId}/messages**`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: childMessages,
          has_more: false,
          oldest_id: null,
        }),
      }),
    );

    await openAppByUrl(page);
    await expect(
      page.getByRole("button", { name: "Plans", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("link", { name: "Plans", exact: true }),
    ).toHaveCount(0);

    const isMobile = (page.viewportSize()?.width ?? 0) <= 768;
    if (isMobile) {
      await page.getByRole("button", { name: "Toggle sidebar" }).click();
    }
    await page.getByText(parentTitle).first().click();

    if (isMobile) {
      const progressChip = page.getByRole("button", {
        name: "Open plan — 5 left",
      });
      await expect(progressChip).toBeVisible();
      await progressChip.click();
      await expect(
        page.getByRole("dialog", { name: "Plan — 5 left" }),
      ).toBeVisible();
    } else {
      const planSection = page.getByRole("button", {
        name: "Plan",
        exact: true,
      });
      // The right panel slides off-screen when closed; an off-screen button is
      // still "visible" to the locator, so judge by its place in the viewport.
      const inView = async () => {
        const box = await planSection.boundingBox();
        const width = page.viewportSize()?.width ?? 0;
        return !!box && box.x >= 0 && box.x + box.width <= width;
      };
      await expect(planSection).toBeAttached();
      if (!(await inView())) {
        await page.getByRole("button", { name: "Toggle panel" }).click();
      }
      await expect.poll(inView).toBe(true);
    }

    const row = (title: string) =>
      page.getByRole("listitem").filter({ hasText: title }).first();
    // A row carries one server-owned state word. Lifecycle detail and
    // executor attribution stay in the expanded details instead of chrome.
    const status = (title: string, text: string) =>
      expect(row(title).getByText(text, { exact: true })).toBeVisible();
    await status(delegatedTitle, "Working");
    await status("Wire the right pane", "Working");
    await status("Verify responsive layout", "Working");
    await status("Await required fixture", "Waiting");
    await status("Parent harvest", "Working");
    await expect(
      row("Await required fixture").getByText(
        "Fixture owner has not published it yet.",
      ),
    ).toHaveCount(0);
    await expect(
      row(delegatedTitle).getByRole("button", { name: "Discussion archive" }),
    ).toBeVisible();

    // Settled work is always visible; there is no separate toggle to reveal it.
    // A settled row names its outcome ("Done"), and its subline repeats the word with the date.
    await expect(row("Map current behavior").locator(".row-status")).toHaveText("Done");

    const inlineToggle = row(delegatedTitle).getByRole("button", {
      name: "active subagent, 1 subtask left",
    });
    await inlineToggle.click();
    await expect(inlineToggle).toHaveAttribute("aria-expanded", "true");
    await status("Inspect projection contract", "Working");
    await saveEvidenceScreenshot(page, testInfo, "contextual-plan-pane", {
      fullPage: false,
    });

    const openChild = page.getByRole("button", {
      name: `Open ${delegatedTitle} subagent beside this thread`,
    });
    await openChild.click();
    await expect(
      page.getByText("Child is inspecting the projection contract."),
    ).toBeVisible();

    const isCompact = (page.viewportSize()?.width ?? 0) < 788;
    if (isCompact) {
      await expect(page.getByRole("button", { name: "Chat" })).toBeVisible();
      await expect(
        page.getByLabel("Subagent conversation").getByRole("textbox", { name: "Message CORSIA" }),
      ).toBeVisible();
      await expect(page.getByRole("textbox", { name: "Message CORSIA" })).toHaveCount(1);
      await saveEvidenceScreenshot(
        page,
        testInfo,
        "contextual-subagent-leaf-mobile",
        { fullPage: false },
      );
      await page.getByRole("button", { name: "Chat" }).click();
    } else {
      await expect(
        page.getByRole("tab", { name: delegatedTitle }),
      ).toBeVisible();
      await expect(
        page.getByLabel("Subagent conversation").getByRole("textbox", { name: "Message CORSIA" }),
      ).toBeVisible();
      await expect(
        page.getByRole("separator", { name: "Resize artifact panel" }),
      ).toBeVisible();
      await saveEvidenceScreenshot(
        page,
        testInfo,
        "contextual-subagent-leaf-desktop",
        { fullPage: false },
      );
      await page
        .getByRole("button", { name: `Close ${delegatedTitle}` })
        .click();
    }

    if (isMobile) {
      await expect(page.getByRole("dialog", { name: "Plan — 5 left" })).toBeVisible();
      const returnedInlineToggle = row(delegatedTitle).getByRole("button", {
        name: "active subagent, 1 subtask left",
      });
      await returnedInlineToggle.click();
      await expect(
        page.getByRole("button", {
          name: `Open ${delegatedTitle} subagent beside this thread`,
        }),
      ).toBeVisible();
    } else {
      // The pane is rebuilt when the surface returns to chat (the leaf's
      // comments pane stood in its place), so what returns is the opener's
      // focus, not the tree's expansion.
      const returnedOpenChild = page.getByRole("button", {
        name: `Open ${delegatedTitle} subagent beside this thread`,
      });
      await expect(returnedOpenChild).toBeVisible();
      await expect(row(delegatedTitle).getByRole("button", {
        name: `${delegatedTitle} Working`,
        exact: true,
      })).toBeFocused();
    }
  });
});
