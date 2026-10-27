import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, openAppByUrl, TEST_TOKEN } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { injectMessage } from '../support/mock';

const out = process.env.PERSON_STEP_LIVE_OUT;

function seed(thread: string, count: number): { plan: string; nodes: string[] } {
  const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
  const workspace = process.env.CORSIA_E2E_WORKSPACE;
  const state = process.env.CORSIA_E2E_STATE_DIR;
  if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Disposable e2e-local state required');
  const script = [
    'import json, sys',
    'from infra.database import get_db',
    'from plans import service',
    'db = get_db()',
    'plan = service.ensure_plan("thread", sys.argv[1], db=db)',
    'nodes = [service.add_node(plan.id, kind="task", title=f"Run proof {i+1}", acceptance_criteria="The proof passes", description="Run the proof. Already tried: opened the instructions.", assignee="user", size="S", db=db).id for i in range(int(sys.argv[2]))]',
    'db.commit()',
    'print(json.dumps({"plan": plan.id, "nodes": nodes}))',
  ].join('\n');
  return JSON.parse(execFileSync(python, ['-c', script, thread, String(count)], {
    cwd: path.resolve('..'), encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
  }));
}

function seedSpecial(thread: string, kind: 'decision' | 'system'): void {
  const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
  const workspace = process.env.CORSIA_E2E_WORKSPACE;
  const state = process.env.CORSIA_E2E_STATE_DIR;
  if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Disposable e2e-local state required');
  const script = [
    'import sys',
    'from infra.database import get_db',
    'from plans import service, decisions, system_user_steps',
    'from plans.boundary import PlanActor',
    'db = get_db()',
    'thread, kind = sys.argv[1:]',
    'if kind == "system":',
    '  step = system_user_steps.open_user_step(db, thread, key="test", title="System step", description="Complete this step", provenance="e2e")',
    '  system_user_steps.settle_user_step(db, step, evidence="Completed", provenance="e2e")',
    'else:',
    '  plan = service.ensure_plan("thread", thread, db=db)',
    '  source = service.add_node(plan.id, kind="task", title="Decision source", acceptance_criteria="Done", db=db)',
    '  user, project = db.execute("SELECT user_id, project_id FROM threads WHERE id=?", (thread,)).fetchone()',
    '  actor = PlanActor(thread, user, project)',
    '  d = decisions.draft(plan.id, source_node_id=source.id, mutation_id="draft", actor=actor, provenance="e2e", db=db, title="Example decision", context="Choose an option", tradeoff="Two options", recommendation="A", rationale="Preferred", options=[{"id":"A", "label":"One", "detail":"First"}, {"id":"B", "label":"Two", "detail":"Second"}], other_enabled=True).result["decision"]',
    '  d = decisions.parent_review(d["id"], expected_revision=d["revision"], mutation_id="review", actor=actor, provenance="e2e", db=db).result["decision"]',
    '  decisions.raise_to_user(d["id"], expected_revision=d["revision"], mutation_id="raise", actor=actor, provenance="e2e", db=db)',
    'db.commit()',
  ].join('\n');
  execFileSync(python, ['-c', script, thread, kind], {
    cwd: path.resolve('..'), encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
  });
}

for (const kind of ['decision', 'system'] as const) {
  test(`person-step live ${kind} producer`, async ({ page, request }) => {
    await setFocusMode(request, true);
    const title = `Live ${kind} producer`;
    const thread = await createSession(request, title);
    await injectMessage(request, thread, { role: 'assistant', content: 'The proof is ready for you.' }, TEST_TOKEN);
    seedSpecial(thread, kind);
    await openAppByUrl(page);
    if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
    const loaded = page.waitForResponse(r => r.url().includes(`/threads/${thread}/messages`));
    await page.getByText(title).first().click();
    await loaded;
    if (kind === 'decision') await expect(page.locator('[data-person-step]')).toHaveCount(0);
    else await expect(page.locator('[data-person-step]')).toHaveAttribute('data-step-state', 'done');
  });
}

for (const action of ['done', 'closed'] as const) {
  test(`person-step live Do and ${action}`, async ({ page, request }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await setFocusMode(request, true);
    const title = `Live action ${action}`;
    const thread = await createSession(request, title);
    await injectMessage(request, thread, { role: 'assistant', content: 'The proof is ready for you.' }, TEST_TOKEN);
    seed(thread, 1);
    await openAppByUrl(page);
    const loaded = page.waitForResponse(r => r.url().includes(`/threads/${thread}/messages`));
    await page.getByText(title).first().click();
    await loaded;
    const chip = page.locator('[data-person-step]').first();
    await expect(chip).toHaveAttribute('data-step-state', 'open');
    await chip.getByRole('button', { name: /Do: Run proof 1/ }).click();
    await expect(page.getByRole('region', { name: 'Run proof 1 details' })).toBeVisible();
    const row = page.locator('.tree-row').filter({ hasText: 'Run proof 1' }).first();
    await row.click({ button: 'right' });
    await page.getByRole('menuitem', { name: action === 'done' ? 'Mark done' : 'Won’t do' }).click();
    await expect(chip).toHaveAttribute('data-step-state', action);
    await expect(chip.getByRole('button', { name: /Do: Run proof 1/ })).toBeHidden();
  });
}

for (const width of [393, 1440]) {
  for (const scene of ['waiting', 'group', 'done', 'closed'] as const) {
    test(`person-step live ${scene} ${width}`, async ({ page, request }, testInfo) => {
      if (!out) throw new Error('PERSON_STEP_LIVE_OUT is required');
      const output = path.join(out, testInfo.project.name);
      fs.mkdirSync(output, { recursive: true });
      await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
      await page.addInitScript(() => localStorage.setItem('corsia_theme', 'light'));
      await setFocusMode(request, true);
      const title = `Live person step ${scene} ${width}`;
      const thread = await createSession(request, title);
      await injectMessage(request, thread, { role: 'assistant', content: 'The proof is ready for you.' }, TEST_TOKEN);
      const fixture = seed(thread, scene === 'group' ? 2 : 1);
      if (scene === 'done' || scene === 'closed') {
        const response = await request.post(`/api/plans/${fixture.plan}/mutations`, {
          headers: { Authorization: `Bearer ${TEST_TOKEN}` },
          data: { operation: scene === 'done' ? 'mark_done' : 'close_node', mutation_id: crypto.randomUUID(),
            params: { node_id: fixture.nodes[0], expected_revision: 1, ...(scene === 'closed' ? { reason: 'Not needed' } : {}) } },
        });
        expect(response.ok(), await response.text()).toBeTruthy();
      }
      await openAppByUrl(page);
      if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
      const loaded = page.waitForResponse(r => r.url().includes(`/threads/${thread}/messages`));
      await page.getByText(title).first().click();
      await loaded;
      const row = page.locator('[data-person-step]').first();
      await expect(row).toBeVisible();
      if (scene === 'group') await expect(row).toContainText('+1 more');
      else if (scene === 'waiting') await expect(row).toContainText('Needs you');
      else await expect(row).toHaveAttribute('data-step-state', scene);
      await page.screenshot({ path: path.join(output, `${scene}-${width}.png`), animations: 'disabled' });
    });
  }
}
