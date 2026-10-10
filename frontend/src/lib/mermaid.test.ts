import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the heavy mermaid module so the dynamic import is fast and deterministic.
// `render` echoes a tiny SVG on success and throws on a sentinel to exercise the
// graceful-error path.
const render = vi.fn(async (_id: string, code: string) => {
	if (code.includes('THROW')) throw new Error('Parse error on line 1');
	return { svg: `<svg data-code="${code.length}"><g/></svg>` };
});
const initialize = vi.fn();
vi.mock('mermaid', () => ({ default: { initialize, render } }));

import { renderMermaid, mountMermaid, hydrateMermaid } from './mermaid';
import { renderMarkdown } from './markdown';

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
	render.mockClear();
	initialize.mockClear();
});

describe('renderMermaid', () => {
	it('returns sanitized SVG for a valid diagram and initializes with strict security', async () => {
		const res = await renderMermaid('stateDiagram-v2\n [*] --> A');
		expect(res.ok).toBe(true);
		if (res.ok) expect(res.svg).toContain('<svg');
		// strict security level is the XSS guard this module owns
		expect(initialize).toHaveBeenCalled();
		expect(initialize.mock.calls[0][0]).toMatchObject({ securityLevel: 'strict', startOnLoad: false });
	});

	it('degrades gracefully (never throws) on a malformed diagram', async () => {
		const res = await renderMermaid('THROW garbage');
		expect(res.ok).toBe(false);
		if (!res.ok) expect(res.error).toContain('Parse error');
	});

	it('treats an empty diagram as an error without invoking mermaid', async () => {
		const res = await renderMermaid('   \n  ');
		expect(res.ok).toBe(false);
		expect(render).not.toHaveBeenCalled();
	});
});

describe('mountMermaid', () => {
	it('renders the diagram SVG into the host element', async () => {
		const el = document.createElement('div');
		const mount = mountMermaid(el, 'graph LR\n a --> b');
		expect(el.classList.contains('mermaid-diagram')).toBe(true);
		await vi.waitFor(() => expect(el.querySelector('svg')).not.toBeNull());
		mount.destroy();
	});

	it('shows an inline error card (as text, not HTML) for a bad diagram', async () => {
		const el = document.createElement('div');
		const mount = mountMermaid(el, 'THROW <img src=x onerror=alert(1)>');
		await vi.waitFor(() => expect(el.querySelector('.mermaid-error')).not.toBeNull());
		// the malformed source is rendered as text — it must not become a live <img>
		expect(el.querySelector('img')).toBeNull();
		expect(el.querySelector('.mermaid-error__src')?.textContent).toContain('onerror');
		mount.destroy();
	});

	it('stops painting after destroy()', async () => {
		const el = document.createElement('div');
		const mount = mountMermaid(el, 'graph LR\n a --> b');
		mount.destroy();
		await Promise.resolve();
		expect(el.querySelector('svg')).toBeNull();
	});
});

describe('hydrateMermaid action', () => {
	function makeRoot(): HTMLElement {
		const root = document.createElement('div');
		root.innerHTML =
			'<p>before</p>' +
			'<div class="code-block"><pre><code class="language-mermaid">graph LR\n a --> b</code></pre></div>' +
			'<p>after</p>';
		document.body.append(root);
		return root;
	}

	it('replaces a rendered ```mermaid block with a live diagram', async () => {
		const root = makeRoot();
		const handle = hydrateMermaid(root, 'k1')!;
		await flush();
		expect(root.querySelector('code.language-mermaid')).toBeNull(); // original block swapped out
		expect(root.querySelector('.mermaid-diagram svg')).not.toBeNull();
		expect(root.textContent).toContain('before');
		expect(root.textContent).toContain('after');
		handle?.destroy?.();
		root.remove();
	});

	it('keeps live diagrams on a spurious update with no fresh blocks', async () => {
		const root = makeRoot();
		const handle = hydrateMermaid(root, 'k1')!;
		await flush();
		const svgBefore = root.querySelector('.mermaid-diagram svg');
		expect(svgBefore).not.toBeNull();
		handle?.update?.('k2'); // param changed but DOM was not re-rendered → no new code blocks
		await flush();
		expect(root.querySelector('.mermaid-diagram svg')).toBe(svgBefore); // not torn down
		handle?.destroy?.();
		root.remove();
	});

	// Contract test: drive the REAL renderMarkdown output (not hand-built HTML)
	// through the action, so a change to markdown.ts's fenced-code structure
	// (e.g. the concurrent sanitization work) fails loudly here instead of
	// silently breaking hydration in production.
	it('hydrates a ```mermaid block emitted by the real renderMarkdown', async () => {
		const root = document.createElement('div');
		root.className = 'md-content';
		root.innerHTML = renderMarkdown('# Doc\n\n```mermaid\nstateDiagram-v2\n  [*] --> A\n```\n');
		document.body.append(root);
		// Precondition the action depends on: renderMarkdown emits code.language-mermaid
		expect(root.querySelector('code.language-mermaid')).not.toBeNull();
		const handle = hydrateMermaid(root, 'real')!;
		await flush();
		expect(root.querySelector('.mermaid-diagram svg')).not.toBeNull();
		expect(root.querySelector('code.language-mermaid')).toBeNull(); // swapped out
		handle?.destroy?.();
		root.remove();
	});
});
