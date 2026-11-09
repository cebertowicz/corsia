import fs from 'node:fs';
import path from 'node:path';

// Use 16px for em/rem thresholds; container font sizes can differ.
export function scanBreakpoints(root) {
  const queries = { media: new Set(), container: new Set() };
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
      for (const query of source.matchAll(/@(media|container)\b([^{};]*)\{/g)) {
        const conditions = [
          /\b(?:min-|max-)?width\s*(?::|[<>]=?)\s*([\d.]+)(px|rem|em)\b/g,
          /\b([\d.]+)(px|rem|em)\s*[<>]=?\s*width\b/g,
        ];
        for (const condition of conditions) for (const value of query[2].matchAll(condition)) {
          queries[query[1]].add(Number(value[1]) * (value[2] === 'px' ? 1 : 16));
        }
      }
    }
  }
  visit(root);
  return Object.fromEntries(Object.entries(queries).map(([key, values]) => [key, [...values].sort((a, b) => a - b)]));
}

export function captureWidths(breakpoints) {
  const widths = new Set([393, 1440]);
  for (const value of [...breakpoints.media, ...breakpoints.container]) {
    widths.add(Math.max(1, Math.ceil(value) - 1));
    if (Number.isInteger(value)) widths.add(value);
    widths.add(Math.floor(value) + 1);
  }
  return [...widths].sort((a, b) => a - b);
}
