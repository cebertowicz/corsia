import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';

test.afterEach(async ({ request }) => {
 await setFocusMode(request, true);
});

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const example = 'shared/research/reviews/APPROVE.md';

for (const theme of ['light', 'dark'] as const) {
 test(`relative path runtime ${theme}`, async ({ page, request }, info) => {
  test.setTimeout(60000);
  if (process.env.CORSIA_E2E_MOCK !== '1' || !process.env.CORSIA_E2E_STATE_DIR || !process.env.CORSIA_E2E_WORKSPACE) throw new Error('Requires isolated mock roots');
  const mobile = info.project.name === 'chromium-mobile';
  await setFocusMode(request, false);
  const thread = await createSession(request, `Relative runtime ${theme} ${info.project.name}`);
  const workspace = process.env.CORSIA_E2E_WORKSPACE;
  const ref = theme === 'light' && !mobile ? example : `runtime-${theme}-${mobile ? 'phone' : 'desktop'}/${example}`;
  const actual = path.join(workspace, ref);
  fs.mkdirSync(path.dirname(actual), { recursive: true });
  if (ref === example) fs.writeFileSync(path.join(workspace, 'shared/research/INDEX.md'), '# Research\n');
  const unique = `Unique runtime body ${theme} ${mobile ? 'phone' : 'desktop'}`;
  fs.writeFileSync(actual, `# Approval document\n\n${unique}\n`);
  const others = Array.from({ length: 10 }, (_, i) => `runtime-${theme}-${mobile ? 'phone' : 'desktop'}/files/${i === 8 ? 'a-long-document-name-for-scrolling-and-truncation' : `file${i}`}.md`);
  for (const [i, name] of others.entries()) {
   const file = path.join(workspace, name);
   fs.mkdirSync(path.dirname(file), { recursive: true });
   fs.writeFileSync(file, `# Other ${i}\n`);
  }
  const twinRef = `runtime-${theme}-${mobile ? 'phone' : 'desktop'}/twin/APPROVE.md`;
  const twin = path.join(workspace, twinRef);
  fs.mkdirSync(path.dirname(twin), { recursive: true });
  const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: twin, text: '# Registered twin\n', thread_id: thread, create: true } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  const text = `References: ${others.join(' ')}. Open ${ref} and \`${ref}\`. Also ${twinRef}.`;
  execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON!, ['-c', [
   'import sys, uuid', 'from infra.database import get_db', 'from thread.transcript_order import draw_transcript_position',
   'thread, content = sys.argv[1:]', 'db = get_db()', 'seq = draw_transcript_position(db, thread)',
   'db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,datetime(\'now\'),?)", (uuid.uuid4().hex,thread,"assistant",content,"{}",seq))', 'db.commit()',
  ].join('\n'), thread, text], { cwd: path.join(root, 'backend'), env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: process.env.CORSIA_E2E_STATE_DIR, CORSIA_WORKSPACE: workspace } });
  const revision = async () => (await (await request.get('/api/files/revision', { params: { token: TEST_TOKEN, path: actual } })).json());
  expect((await revision()).artifact_id).toBeNull();
  await page.setViewportSize({ width: mobile ? 393 : 1440, height: mobile ? 852 : 900 });
  await page.addInitScript((value) => localStorage.setItem('corsia_theme', value), theme);
  await loginByUrl(page, { waitForComposer: false });
  if (mobile) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.locator('.thread-item').filter({ hasText: `Relative runtime ${theme} ${info.project.name}` }).click();
  const chips = page.locator('.chips button');
  await expect(chips).toHaveCount(12);
  const twins = chips.filter({ hasText: 'APPROVE.md' });
  await expect(twins).toHaveCount(2);
  const target = twins.first();
  await expect(target).not.toHaveAttribute('data-artifact-id');
  await expect(twins.last()).toHaveAttribute('data-artifact-id', /.+/);
  expect((await revision()).artifact_id).toBeNull();
  const out = process.env.CORSIA_E2E_EVIDENCE_DIR!;
  await page.screenshot({ path: path.join(out, `${theme}-${mobile ? '393' : '1440'}-initial.png`), animations: 'disabled' });
  if (mobile) {
   const strip = page.locator('.chips');
   const cdp = await page.context().newCDPSession(page);
   for (let i = 0; i < 12; i++) {
    const stripBox = await strip.boundingBox();
    const targetBox = await target.boundingBox();
    if (!stripBox || !targetBox) throw new Error('Missing chip strip');
    if (targetBox.x >= stripBox.x && targetBox.x + targetBox.width <= stripBox.x + stripBox.width) break;
    const y = stripBox.y + stripBox.height / 2, start = stripBox.x + stripBox.width * .83, end = stripBox.x + stripBox.width * .2;
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: start, y }] });
    for (let j = 1; j <= 8; j++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: start + (end - start) * j / 8, y }] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(400);
   }
   await expect.poll(() => strip.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
   const stripBox = await strip.boundingBox();
   const targetBox = await target.boundingBox();
   if (!stripBox || !targetBox) throw new Error('Missing scrolled target');
   expect(Math.min(stripBox.x + stripBox.width, targetBox.x + targetBox.width) - Math.max(stripBox.x, targetBox.x)).toBeGreaterThanOrEqual(30);
   expect((await revision()).artifact_id).toBeNull();
   await page.screenshot({ path: path.join(out, `${theme}-393-scrolled.png`), animations: 'disabled' });
   await target.tap();
  } else {
   let reached = false;
   for (let i = 0; i < 160; i++) { await page.keyboard.press('Tab'); if (await target.evaluate((el) => document.activeElement === el)) { reached = true; break; } }
   expect(reached).toBe(true);
   expect(await target.evaluate((el) => el.matches(':focus-visible'))).toBe(true);
   await page.screenshot({ path: path.join(out, `${theme}-1440-focused.png`), animations: 'disabled' });
   expect((await revision()).artifact_id).toBeNull();
   if (theme === 'light') await target.click();
   else await page.keyboard.press('Enter');
  }
  await expect(page.getByText(unique)).toBeVisible();
  const opened = await revision();
  expect(opened.artifact_id).toBeTruthy();
  await page.screenshot({ path: path.join(out, `${theme}-${mobile ? '393' : '1440'}-opened.png`), animations: 'disabled' });
  if (mobile) await page.getByRole('button', { name: 'Chat', exact: true }).tap();
  await expect(target).toBeVisible();
  fs.writeFileSync(path.join(out, `${theme}-${mobile ? '393' : '1440'}-receipt.json`), JSON.stringify({ reference: ref, cards: 12, registeredTwin: true, before: null, afterIdentityPresent: !!opened.artifact_id, contentVisible: true, returnedToChat: true, method: mobile ? 'emulated touch' : theme === 'light' ? 'Tab/click' : 'Tab/Enter' }, null, 2));
 });
}
