import fs from 'node:fs';
import path from 'node:path';

// Media-query em/rem units use the initial font size, conventionally 16px.
export function scanBreakpoints(root) {
  const pairs = new Set();
  function add(value, unit, operator) {
    const threshold = Number(value) * (unit === 'px' ? 1 : 16);
    // The first integer viewport at which the condition changes.
    const boundary = operator === '<=' || operator === '>' ? Math.floor(threshold) + 1 : Math.ceil(threshold);
    pairs.add(boundary);
  }
  function visit(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      if (entry.isDirectory()) { visit(file); continue; }
      if (!/\.(css|svelte)$/.test(entry.name)) continue;
      let source = fs.readFileSync(file, 'utf8');
      if (entry.name.endsWith('.svelte')) {
        source = [...source.matchAll(/<style(?:\s[^>]*)?>([\s\S]*?)<\/style>/g)].map(match => match[1]).join('\n');
      }
      source = source.replace(/\/\*[\s\S]*?\*\//g, '');
      for (const query of source.matchAll(/@media\b([^{};]*)\{/g)) {
        for (const match of query[1].matchAll(/\b(min|max)-width\s*:\s*([\d.]+)(px|rem|em)\b/g)) {
          add(match[2], match[3], match[1] === 'min' ? '>=' : '<=');
        }
        for (const match of query[1].matchAll(/\bwidth\s*(<=|>=|<|>)\s*([\d.]+)(px|rem|em)\b/g)) {
          add(match[2], match[3], match[1]);
        }
        for (const match of query[1].matchAll(/\b([\d.]+)(px|rem|em)\s*(<=|>=|<|>)\s*width\b/g)) {
          const reverse = { '<': '>', '<=': '>=', '>': '<', '>=': '<=' };
          add(match[1], match[2], reverse[match[3]]);
        }
      }
    }
  }
  visit(root);
  return [...pairs].sort((a, b) => a - b);
}

export function captureWidths(boundaries) {
  // A phone, a laptop with the sidebar open, and a desktop wide enough for a side column beside a centred canvas.
  const widths = new Set([393, 1440, 1920]);
  for (const boundary of boundaries) {
    if (boundary - 1 >= 320) widths.add(boundary - 1);
    if (boundary >= 320) widths.add(boundary);
  }
  return [...widths].sort((a, b) => a - b);
}
