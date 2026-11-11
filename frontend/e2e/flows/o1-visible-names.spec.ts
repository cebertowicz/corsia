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
    let previous: { width: number; labels: string[]; excluded: boolean } | null = null;
    let font: string | null = null;
    for (let width = 320; width <= 1440; width += 20) {
      await page.setViewportSize({ width, height: 900 }); await page.waitForTimeout(300);
      const result = await page.locator('.document-header').evaluate(header => {
        const control = [...header.querySelectorAll<HTMLElement>('button.identity')].find(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
        const box = control?.getBoundingClientRect();
        const parent = control?.parentElement?.getBoundingClientRect();
        const contained = !!control && !!box && !!parent && getComputedStyle(control).overflowX === 'hidden' && box.left >= parent.left - 1 && box.right <= parent.right + 1;
        const names = [...(control?.querySelectorAll<HTMLElement>('.identity-name') ?? [])].map(name => {
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
          return { text, visible, full: !truncated && visible === text.length, truncated, ellipsis: getComputedStyle(name).textOverflow === 'ellipsis', width: bounds.width, font: getComputedStyle(name).fontSize, top: bounds.top };
        });
        function shown(el: HTMLElement) {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility === 'visible' && rect.right > 0 && rect.left < innerWidth;
        }
        const labels: string[] = [];
        for (const [selector, label] of [['.delivery-toggle > span', 'Deliver immediately'], ['.verification-action button', 'Mark verified']]) {
          if ([...header.querySelectorAll<HTMLElement>(selector)].some(shown)) labels.push(`label:${label}`);
        }
        for (const element of header.querySelectorAll<HTMLElement>('.identity-verified, .document-metadata time, .document-metadata .metadata-secondary')) {
          if (shown(element)) labels.push(`verification:${[...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim()}`);
        }
        for (const name of names) if (name.visible > 0) labels.push(`name:${name.text}`);
        const verification = labels.some(label => label.startsWith('verification:'));
        const compact = !!control?.closest('.identity-age');
        return { contained, names, box: box?.toJSON(), parent: parent?.toJSON(), labels: [...new Set(labels)].sort(), verification, compact };
      });
      const excluded = workspace === 'open' && (width === 1080 || width === 1100);
      const missing = previous && !previous.excluded && !excluded ? previous.labels.filter(label => !result.labels.includes(label)) : [];
      const sizes = result.names.map(name => name.font);
      if (!font && sizes.length) font = sizes[0];
      const uniformFont = sizes.length === 2 && sizes.every(size => size === font);
      const oneRow = !result.compact || result.names.length === 2 && Math.abs(result.names[0].top - result.names[1].top) < 1;
      const verified = !result.verification || result.labels.includes('label:Mark verified');
      const pass = result.contained && (excluded || (result.names.length === 2 && result.names.every(name => name.full || (name.truncated && name.ellipsis && name.visible >= Math.min(10, name.text.length))) && uniformFont && oneRow && verified && missing.length === 0));
      const row = { workspace, root, width, excluded, pass, missing, uniformFont, oneRow, verified, ...result }; appendFileSync(table, `${JSON.stringify(row)}\n`);
      previous = { width, labels: result.labels, excluded };
      await page.screenshot({ path: `${out}/${workspace}-${root}-${width}.png`, clip: { x: 0, y: 0, width, height: 240 }, animations: 'disabled' });
      if (!pass) failures.push(JSON.stringify(row));
    }
  }
  expect(failures, 'names must be fully visible or show at least ten characters before the ellipsis').toEqual([]);
});
