import { test, expect } from './support/test';
import { createSession, openAppByUrl } from './support/corsia';
import { operatorNode, withPlanContract } from '../src/lib/__tests__/fixtures/operatorPlan';
import type { Page, APIRequestContext } from '@playwright/test';

async function openPlan(page: Page, request: APIRequestContext, width: number, refusal = false, nested = false) {
  await page.setViewportSize({ width, height: 900 });
  const title = `drag-reorder-${Date.now()}`;
  const threadId = await createSession(request, title);
  const nodes = ['First task', 'Second task', 'Third task'].map((title, i) => operatorNode({
    id: `task-${i}`, title, user_actions: ['reorder'], updated_at: '2026-08-13T00:00:00Z',
    ...(nested ? { parent_id: 'parent', depth: 1 } : {}),
  }));
  if (nested) nodes.unshift(operatorNode({ id: 'parent', title: 'Parent task', depth: 0 }));
  const entry = withPlanContract({
    plan: { id: `plan-${threadId}`, owner_kind: 'thread', owner_id: threadId, state: 'active', revision: 1, updated_at: '2026-08-13T00:00:00Z' },
    owner: { label: 'Conversation', project_id: null, thread_id: threadId },
    progress: { done: 0, total: 3, open: 3, liveness: 'advancing' },
    current_node_ids: [], next_node_ids: [], nodes,
  });
  let calls: unknown[] = [];
  await page.route(`**/api/plans/operator/threads/${threadId}**`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    contract_version: 2, as_of: '2026-08-13T00:00:00Z', target: { kind: 'thread', id: threadId, label: 'Conversation' },
    revision_token: 'drag-test', attention: { awaiting_count: 0 }, plans: [{ ...entry, nodes }],
  }) }));
  await page.route(`**/api/plans/plan-${threadId}/mutations`, async (route) => {
    const body = route.request().postDataJSON(); calls.push(body);
    if (refusal) return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Test refusal detail' }) });
    const moved = nodes.find((node) => node.id === body.params.node_id)!;
    const indexes = nodes.map((node, index) => node.parent_id === moved.parent_id ? index : -1).filter((index) => index >= 0);
    const siblings = indexes.map((index) => nodes[index]);
    const from = siblings.findIndex((node) => node.id === moved.id);
    siblings.splice(body.params.order_key, 0, ...siblings.splice(from, 1));
    indexes.forEach((index, i) => { nodes[index] = siblings[i]; });
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });
  await openAppByUrl(page);
  if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByText(title).first().click();
  if (width < 768) await page.getByRole('button', { name: /^Open plan —/ }).click();
  else {
    const panel = page.getByRole('button', { name: 'Plan', exact: true });
    const box = await panel.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > width) await page.getByRole('button', { name: 'Toggle panel' }).click();
  }
  const tree = page.locator('.plan-tree').first();
  if (nested) await tree.getByRole('button', { name: /^Expand Parent task/ }).click();
  await expect(tree.getByText('First task')).toBeVisible();
  return { tree, calls, threadId, title };
}

for (const width of [1440, 393]) {
  test.describe(`reorder at ${width}`, () => {
    test.use({ hasTouch: width === 393, isMobile: width === 393 });
    test('drag writes one sibling position and captures the lifted row', async ({ page, request, context }, info) => {
      const { tree, calls, title } = await openPlan(page, request, width);
      const source = tree.locator('[data-node-id="task-0"]');
      const target = tree.locator('[data-node-id="task-2"]');
      // Capture the original hit point before waiting for the drawer's entrance motion.
      const initial = (await source.boundingBox())!;
      const initialHit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('[data-node-id]')?.getAttribute('data-node-id') ?? null, { x: initial.x + initial.width / 2, y: initial.y + initial.height / 2 });
      await source.hover({ trial: true });
      await target.hover({ trial: true });
      const a = (await source.boundingBox())!, b = (await target.boundingBox())!;
      if (width === 393) {
        const oldPointHit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('[data-node-id]')?.getAttribute('data-node-id') ?? null, { x: initial.x + initial.width / 2, y: initial.y + initial.height / 2 });
        console.log('touch geometry', JSON.stringify({ initial, initialHit, settled: a, oldPointHit }));
      }
      if (width === 393) {
        const cdp = await context.newCDPSession(page);
        await source.evaluate((row) => {
          for (const name of ['pointerdown', 'pointermove', 'pointercancel']) {
            row.addEventListener(name, () => { row.setAttribute(`data-test-${name}`, 'seen'); });
          }
        });
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: a.x + a.width / 2, y: a.y + a.height / 2 }] });
        await expect(source).toHaveAttribute('data-test-pointerdown', 'seen');
        await page.waitForTimeout(650);
        await expect(page.getByRole('menu')).toHaveCount(0);
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: b.x + b.width / 2, y: b.y + b.height * 0.75 }] });
        await expect(source).not.toHaveAttribute('data-test-pointercancel', 'seen');
        await expect(source).toHaveAttribute('data-test-pointermove', 'seen');
        await expect(source).toHaveClass(/dragging/);
        await page.screenshot({ path: info.outputPath(`drag-${width}.png`) });
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      } else {
        await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2); await page.mouse.down();
        await page.mouse.move(b.x + b.width / 2, b.y + b.height * 0.75, { steps: 8 });
        await expect(source).toHaveClass(/dragging/);
        await page.screenshot({ path: info.outputPath(`drag-${width}.png`) });
        await page.mouse.up();
      }
      await expect.poll(() => calls.length).toBe(1);
      expect(calls[0]).toMatchObject({ operation: 'reorder', params: { node_id: 'task-0', expected_revision: 1, order_key: 2 } });
      await expect.poll(() => tree.locator('[data-node-id]').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-node-id')))).toEqual(['task-1', 'task-2', 'task-0']);
      await expect(page.getByText('First task moved to 3 of 3')).toHaveCount(1);
      if (width === 1440) {
        await page.reload();
        await page.getByText(title).first().click();
        await expect.poll(() => tree.locator('[data-node-id]').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-node-id')))).toEqual(['task-1', 'task-2', 'task-0']);
      }
    });
    test('refusal restores the dragged order without overlapping rows', async ({ page, request, context }, info) => {
      const { tree } = await openPlan(page, request, width, true);
      const source = tree.locator('[data-node-id="task-1"]');
      const target = tree.locator('[data-node-id="task-0"]');
      const order = () => tree.locator('[data-node-id]').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-node-id')));
      const before = await order();
      await source.hover({ trial: true });
      await target.hover({ trial: true });
      const a = (await source.boundingBox())!, b = (await target.boundingBox())!;
      if (width === 393) {
        const cdp = await context.newCDPSession(page);
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: a.x + a.width / 2, y: a.y + a.height / 2 }] });
        await page.waitForTimeout(650);
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: b.x + b.width / 2, y: b.y + b.height / 4 }] });
        await expect(source).toHaveClass(/dragging/);
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      } else {
        await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2); await page.mouse.down();
        await page.mouse.move(b.x + b.width / 2, b.y + b.height / 4, { steps: 8 });
        await expect(source).toHaveClass(/dragging/);
        await page.mouse.up();
      }
      const error = page.getByRole('alert').filter({ hasText: "Couldn't move this task. Try again." });
      await expect(error).toBeVisible();
      await expect(page.getByRole('menu')).toHaveCount(0);
      const hit = (selector: string) => page.locator(selector).evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const found = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return { matches: found === element || element.contains(found), hit: found ? `${found.tagName}.${found.className}` : null, text: found?.textContent?.trim().slice(0, 60) ?? null, top: rect.top };
      });
      // The error line is never covered, even while the rows glide back.
      const glidingErrorHit = await hit('[role="alert"] span');
      expect(glidingErrorHit.matches).toBe(true);
      // Rows cross mid-glide by design; the row probe reads the settled state.
      await expect.poll(() => tree.evaluate((element) => element.getAnimations({ subtree: true }).filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().endTime !== Infinity).length)).toBe(0);
      const errorHit = await hit('[role="alert"] span');
      const rowHit = await hit('[data-node-id="task-0"] .row-title');
      console.log('hit probe', width, JSON.stringify({ glidingErrorHit, errorHit, rowHit }));
      expect(errorHit.matches).toBe(true);
      expect(rowHit.matches).toBe(true);
      await expect.poll(order).toEqual(before);
      await expect(source.locator('.row-body')).toBeFocused();
      await target.hover({ trial: true });
      console.log('refusal evidence', JSON.stringify({ before, after: await order(), errorHit, rowHit }));
      await page.screenshot({ path: info.outputPath(`refusal-${width}.png`) });
      await expect(error.getByRole('button', { name: 'Dismiss' })).toHaveAttribute('title', 'Test refusal detail');
      await error.getByRole('button', { name: 'Dismiss' }).click();
      await expect(error).toHaveCount(0);
    });
  });
}

test('keyboard menu reorders children within their parent', async ({ page, request }) => {
  const { tree, calls } = await openPlan(page, request, 1440, false, true);
  await tree.locator('[data-node-id="task-2"] .row-body').focus();
  await page.keyboard.press('Shift+F10');
  await page.getByRole('menuitem', { name: 'Move to top' }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0]).toMatchObject({ operation: 'reorder', params: { node_id: 'task-2', order_key: 0 } });
  await expect.poll(() => tree.locator('[data-node-id]').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-node-id')))).toEqual(['parent', 'task-2', 'task-0', 'task-1']);
});

test('keyboard menu moves a row to the top', async ({ page, request }) => {
  const { tree, calls } = await openPlan(page, request, 1440);
  await tree.locator('[data-node-id="task-2"] .row-body').focus();
  await page.keyboard.press('Shift+F10');
  await page.getByRole('menuitem', { name: 'Move to top' }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0]).toMatchObject({ operation: 'reorder', params: { node_id: 'task-2', order_key: 0, expected_revision: 1 } });
  await expect(tree.locator('[data-node-id="task-2"] .row-body')).toBeFocused();
});
