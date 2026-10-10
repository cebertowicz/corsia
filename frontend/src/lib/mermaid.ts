/**
 * Mermaid diagram rendering for CORSIA's document + markdown surfaces.
 *
 * Mermaid is a heavy dependency, so the library is dynamically imported on first
 * use and never bundled into the initial app load. Its SVG output bypasses the
 * canonical `renderMarkdown` sanitizer, so this module owns its own output
 * safety: it initializes mermaid with `securityLevel: 'strict'`, which sanitizes
 * diagram labels and strips scriptable content before returning the SVG.
 *
 * Two consumers share the core renderer:
 *  - the CodeMirror live-preview block widget in `cm/markdown-live.ts` (the
 *    documents viewer), and
 *  - the static-HTML `hydrateMermaid` action (rendered `renderMarkdown` surfaces).
 */
import { get } from 'svelte/store';
import type { Action } from 'svelte/action';
import { theme } from '$lib/stores/theme';

// Fallback font stack for environments without a live DOM (SSR/tests). At
// runtime the real value is read from the --font-sans design token so the
// layout font mermaid measures with can't drift from the token.
const FALLBACK_FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter', system-ui, sans-serif";

/** Resolve the diagram font from the --font-sans token (single source of truth). */
function diagramFont(): string {
	if (typeof document !== 'undefined') {
		const v = getComputedStyle(document.documentElement).getPropertyValue('--font-sans').trim();
		if (v) return v;
	}
	return FALLBACK_FONT;
}

type MermaidModule = (typeof import('mermaid'))['default'];

let loader: Promise<MermaidModule> | null = null;
let configuredTheme: 'dark' | 'light' | null = null;
let renderSeq = 0;

/** Lazy-load mermaid once. A failed load clears the cache so a later call retries. */
function loadMermaid(): Promise<MermaidModule> {
	if (!loader) {
		loader = import('mermaid')
			.then((m) => m.default)
			.catch((e) => {
				loader = null;
				throw e;
			});
	}
	return loader;
}

/** (Re)initialize mermaid for the active theme. Only re-inits when the theme changes. */
function ensureConfigured(mermaid: MermaidModule, resolved: 'dark' | 'light') {
	if (configuredTheme === resolved) return;
	mermaid.initialize({
		startOnLoad: false,
		securityLevel: 'strict',
		theme: resolved === 'dark' ? 'dark' : 'default',
		fontFamily: diagramFont()
	});
	configuredTheme = resolved;
}

export type MermaidResult = { ok: true; svg: string } | { ok: false; error: string };

/**
 * Render one mermaid diagram to a sanitized SVG string. Never throws — a
 * malformed diagram resolves to `{ ok: false, error }` so callers can degrade
 * gracefully instead of blanking the surrounding document.
 */
export async function renderMermaid(code: string): Promise<MermaidResult> {
	const src = code.trim();
	if (!src) return { ok: false, error: 'Empty diagram' };
	const id = `mmd-${++renderSeq}`;
	try {
		const mermaid = await loadMermaid();
		ensureConfigured(mermaid, get(theme));
		const { svg } = await mermaid.render(id, src);
		return { ok: true, svg };
	} catch (e) {
		removeOrphan(id);
		return { ok: false, error: e instanceof Error ? e.message : String(e) };
	}
}

/** mermaid can leave a temporary measuring node in the DOM if `render()` throws. */
function removeOrphan(id: string) {
	if (typeof document === 'undefined') return;
	document.getElementById(id)?.remove();
	document.getElementById('d' + id)?.remove();
}

export interface MermaidMount {
	/** Cancel pending work and detach the theme subscription. */
	destroy(): void;
}

/**
 * Render `code` into `el` and keep it in sync with the active theme. Returns a
 * controller whose `destroy()` cancels in-flight renders and unsubscribes.
 * Shared by the CodeMirror widget and the static `hydrateMermaid` action.
 */
export function mountMermaid(el: HTMLElement, code: string): MermaidMount {
	let disposed = false;
	let token = 0;
	el.classList.add('mermaid-diagram');

	const paint = async () => {
		const mine = ++token;
		const res = await renderMermaid(code);
		if (disposed || mine !== token) return; // a newer paint (or destroy) superseded us
		if (res.ok) {
			el.classList.remove('is-error');
			el.innerHTML = res.svg;
		} else {
			el.classList.add('is-error');
			el.replaceChildren(errorNode(res.error, code));
		}
	};

	// `theme.subscribe` fires synchronously with the current value (the first
	// paint) and again on every theme change (re-render so colors track the theme).
	const unsub = theme.subscribe(() => {
		if (!disposed) void paint();
	});

	return {
		destroy() {
			disposed = true;
			unsub();
		}
	};
}

/**
 * Inline, non-destructive error card. The diagram source and the error message
 * are set as text (never HTML), so a malformed diagram can't become an injection
 * vector and the surrounding document keeps rendering.
 */
function errorNode(message: string, code: string): HTMLElement {
	const box = document.createElement('div');
	box.className = 'mermaid-error';

	const label = document.createElement('div');
	label.className = 'mermaid-error__label';
	label.textContent = 'Diagram error';

	const msg = document.createElement('div');
	msg.className = 'mermaid-error__msg';
	msg.textContent = message;

	const pre = document.createElement('pre');
	pre.className = 'mermaid-error__src';
	pre.textContent = code.trim();

	box.append(label, msg, pre);
	return box;
}

/**
 * Svelte action for a container holding `{@html renderMarkdown(...)}` output.
 * Finds rendered ```mermaid code blocks and swaps each for a live diagram. Pass
 * the rendered source as the action parameter so a content change re-runs the
 * pass (`use:hydrateMermaid={bodyHtml}`).
 *
 * Each `{@html}` update replaces the container's innerHTML wholesale (Svelte
 * destroys the previously-inserted diagram holders), so a re-run necessarily
 * re-renders every diagram. That's correct for static/occasional content; for a
 * high-frequency streaming surface the caller should hydrate only the final
 * content (or debounce the bound value) rather than every partial frame.
 */
export const hydrateMermaid: Action<HTMLElement, unknown> = (root) => {
	let mounts: { mount: MermaidMount; holder: HTMLElement }[] = [];

	const clear = () => {
		for (const { mount } of mounts) mount.destroy();
		mounts = [];
	};

	const run = () => {
		const blocks = root.querySelectorAll('code.language-mermaid');
		// A re-run with no fresh mermaid blocks but live diagrams still mounted is a
		// spurious update (the param changed without an {@html} swap) — keep the
		// existing diagrams rather than tearing them down and rendering nothing.
		if (blocks.length === 0 && mounts.some((m) => m.holder.isConnected)) return;
		clear();
		for (const codeEl of blocks) {
			const source = codeEl.textContent ?? '';
			const block = codeEl.closest('.code-block') ?? codeEl.closest('pre') ?? codeEl;
			const holder = document.createElement('div');
			block.replaceWith(holder);
			mounts.push({ mount: mountMermaid(holder, source), holder });
		}
	};

	// Defer so the {@html} children are in the DOM before we scan for them.
	queueMicrotask(run);

	return {
		update: () => queueMicrotask(run),
		destroy: clear
	};
};
