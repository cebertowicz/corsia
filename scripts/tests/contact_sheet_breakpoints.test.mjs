import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { captureWidths, scanBreakpoints } from '../../frontend/e2e/contact-sheet/breakpoints.mjs';

test('captures the integer viewports across media-query transitions only', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'contact-breakpoints-'));
  try {
    fs.writeFileSync(path.join(root, 'layout.css'), `
      /* @media (max-width: 500px) {} */
      @media (min-height: 200px) and (max-width: 48rem) {}
      @media (min-width: 48.0625rem) {}
      @media (max-width: 319px) {}
      @media (400px < width <= 500px) {}
      @container panel (max-width: 600px) {}`);
    fs.writeFileSync(path.join(root, 'component.svelte'), '<script>const sample = "@media (max-width: 900px) {";</script><style>@media (width < 32.5rem) {}</style>');
    const boundaries = scanBreakpoints(root);
    assert.deepEqual(boundaries, [320, 401, 501, 520, 769]);
    assert.deepEqual(captureWidths(boundaries), [320, 393, 400, 401, 500, 501, 519, 520, 768, 769, 1440, 1920]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
