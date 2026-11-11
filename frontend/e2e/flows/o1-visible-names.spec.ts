import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

const out = process.env.O1_SWEEP_OUT!;
if (!out) throw new Error('Set O1_SWEEP_OUT to a fresh evidence directory');
for (const workspace of ['closed', 'open']) test(`visible names Workspace ${workspace}`, async ({ page, request }) => {
  test.setTimeout(900_000); mkdirSync(out, { recursive: true });
  const table = `${out}/${workspace}.jsonl`; writeFileSync(table, '');
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false); await seed(page, request);
  if (workspace === 'closed') await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
  const failures: string[] = [];
  for (const root of [15, 16, 17]) {
    await page.evaluate(size => document.documentElement.style.fontSize = `${size}px`, root);
    for (let width = 320; width <= 1440; width += 20) {
      await page.setViewportSize({ width, height: 900 }); await page.waitForTimeout(300);
      const names = await page.locator('.document-header').evaluate(header => {
        const control = [...header.querySelectorAll<HTMLElement>('button.identity')].find(e => e.getBoundingClientRect().width > 0 && getComputedStyle(e).visibility !== 'hidden');
        return [...(control?.querySelectorAll<HTMLElement>('.identity-name') ?? [])].map(name => {
          const text = name.textContent ?? ''; const node = name.firstChild; const bounds = name.getBoundingClientRect();
          let left = Math.max(0, bounds.left), right = Math.min(innerWidth, bounds.right);
          for (let ancestor = name.parentElement; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor); const box = ancestor.getBoundingClientRect();
            if (/(hidden|clip|scroll|auto)/.test(style.overflowX)) { left = Math.max(left, box.left); right = Math.min(right, box.right); }
          }
          const truncated = name.scrollWidth > name.clientWidth + 1;
          if (truncated && getComputedStyle(name).textOverflow === 'ellipsis') {
            const canvas = document.createElement('canvas'); const ctx = canvas.getContext('2d')!; ctx.font = getComputedStyle(name).font;
            right -= ctx.measureText('…').width;
          }
          let visible = 0;
          if (node) for (let i = 0; i < text.length; i++) {
            const range = document.createRange(); range.setStart(node, i); range.setEnd(node, i + 1);
            const char = range.getBoundingClientRect(); if (char.left >= left - 0.5 && char.right <= right + 0.5 && char.width > 0) visible++; else break;
          }
          return { text, visible, full: !truncated && visible === text.length, truncated, width: bounds.width };
        });
      });
      const excluded = workspace === 'open' && width >= 1025 && width <= 1372;
      const pass = names.length === 2 && names.every(name => name.full || (name.truncated && name.visible >= Math.min(10, name.text.length)));
      const row = { workspace, root, width, excluded, pass, names }; appendFileSync(table, `${JSON.stringify(row)}\n`);
      await page.screenshot({ path: `${out}/${workspace}-${root}-${width}.png`, clip: { x: 0, y: 0, width, height: 240 }, animations: 'disabled' });
      if (!excluded && !pass) failures.push(JSON.stringify(row));
    }
  }
  expect(failures, 'names must be fully visible or show at least ten characters before the ellipsis').toEqual([]);
});
