import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { captureWidths, scanBreakpoints } from '../../frontend/e2e/contact-sheet/breakpoints.mjs';

test('reads width conditions in styles, excluding comments and unrelated dimensions', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'contact-breakpoints-'));
  try {
    fs.writeFileSync(path.join(root, 'layout.css'), '/* @media (max-width: 20px) {} */\n@media (min-height: 200px) and (max-width: 48rem) {}\n@container panel (400px < width) {}');
    fs.writeFileSync(path.join(root, 'component.svelte'), '<script>const sample = "@media (max-width: 900px) {";</script><style>@container (width < 32.5rem) {}</style>');
    const breakpoints = scanBreakpoints(root);
    assert.deepEqual(breakpoints, { media: [768], container: [400, 520] });
    assert.deepEqual(captureWidths(breakpoints), [393, 399, 400, 401, 519, 520, 521, 767, 768, 769, 1440]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
