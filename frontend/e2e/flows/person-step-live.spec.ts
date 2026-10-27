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

for (const width of [393, 1440]) {
  for (const scene of ['waiting', 'group', 'done', 'closed'] as const) {
    test(`person-step live ${scene} ${width}`, async ({ page, request }) => {
      if (!out) throw new Error('PERSON_STEP_LIVE_OUT is required');
      fs.mkdirSync(out, { recursive: true });
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
      await page.screenshot({ path: path.join(out, `${scene}-${width}.png`), animations: 'disabled' });
    });
  }
}
