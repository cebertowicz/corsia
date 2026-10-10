import { Marked, type RendererObject, type Tokens } from 'marked';
import DOMPurify from 'dompurify';

const LANG_CONFIG: Record<string, { keywords: string[]; commentSingle: string; commentMultiStart: string; commentMultiEnd: string }> = {
	js: { keywords: ['const','let','var','function','return','if','else','for','while','class','import','export','from','default','async','await','new','this','typeof','interface','type','extends','implements','try','catch','throw','switch','case','break','continue','null','undefined','true','false','of','in'], commentSingle: '//', commentMultiStart: '/*', commentMultiEnd: '*/' },
	python: { keywords: ['def','class','return','if','elif','else','for','while','import','from','as','try','except','raise','with','yield','lambda','pass','break','continue','None','True','False','self','async','await','not','and','or','in','is'], commentSingle: '#', commentMultiStart: '/*', commentMultiEnd: '*/' },
	sql: { keywords: ['SELECT','FROM','WHERE','INSERT','INTO','UPDATE','SET','DELETE','CREATE','TABLE','ALTER','ADD','DROP','INDEX','JOIN','LEFT','RIGHT','INNER','ON','AND','OR','NOT','NULL','DEFAULT','PRIMARY','KEY','TEXT','INTEGER','REAL','BLOB','IF','EXISTS','ORDER','BY','GROUP','HAVING','LIMIT','AS','VALUES','DESC','ASC','UNIQUE','CONSTRAINT','FOREIGN','REFERENCES','CASCADE'], commentSingle: '--', commentMultiStart: '/*', commentMultiEnd: '*/' },
	bash: { keywords: ['if','then','else','elif','fi','for','while','do','done','case','esac','function','return','exit','echo','cd','ls','cat','grep','awk','sed','export','source','local','readonly'], commentSingle: '#', commentMultiStart: '', commentMultiEnd: '' },
	json: { keywords: ['true','false','null'], commentSingle: '', commentMultiStart: '', commentMultiEnd: '' },
	css: { keywords: ['import','media','keyframes','from','to'], commentSingle: '', commentMultiStart: '/*', commentMultiEnd: '*/' },
};

const LANG_ALIASES: Record<string, string> = {
	javascript: 'js', ts: 'js', typescript: 'js', jsx: 'js', tsx: 'js', svelte: 'js',
	py: 'python',
	sqlite: 'sql',
	sh: 'bash', zsh: 'bash', shell: 'bash',
	jsonl: 'json',
	scss: 'css',
	html: 'js', xml: 'js', svg: 'js',
};

function highlightCode(code: string, lang: string): string {
	if (!lang) return escapeHtml(code);
	const key = LANG_ALIASES[lang.toLowerCase()] ?? lang.toLowerCase();
	const cfg = LANG_CONFIG[key];
	if (!cfg) return escapeHtml(code);

	const escaped = escapeHtml(code);
	return tokenize(escaped, cfg);
}

function tokenize(code: string, cfg: { keywords: string[]; commentSingle: string; commentMultiStart: string; commentMultiEnd: string }): string {
	const result: string[] = [];
	let i = 0;
	const len = code.length;

	while (i < len) {
		if (cfg.commentSingle && code.startsWith(cfg.commentSingle, i)) {
			const end = code.indexOf('\n', i);
			const slice = end === -1 ? code.slice(i) : code.slice(i, end);
			result.push(`<span class="hl-comment">${slice}</span>`);
			i += slice.length;
			continue;
		}
		if (cfg.commentMultiStart && code.startsWith(cfg.commentMultiStart, i)) {
			const end = code.indexOf(cfg.commentMultiEnd, i + cfg.commentMultiStart.length);
			const slice = end === -1 ? code.slice(i) : code.slice(i, end + cfg.commentMultiEnd.length);
			result.push(`<span class="hl-comment">${slice}</span>`);
			i += slice.length;
			continue;
		}
		if (code[i] === '"' || code[i] === "'" || code[i] === '`') {
			const quote = code[i];
			let j = i + 1;
			while (j < len && code[j] !== quote) {
				if (code[j] === '\\') j++;
				j++;
			}
			j++;
			result.push(`<span class="hl-string">${code.slice(i, j)}</span>`);
			i = j;
			continue;
		}
		if (code.startsWith('&quot;', i)) {
			let j = i + 6;
			while (j < len && !code.startsWith('&quot;', j)) j++;
			j += 6;
			result.push(`<span class="hl-string">${code.slice(i, j)}</span>`);
			i = j;
			continue;
		}
		if (/\d/.test(code[i]) && (i === 0 || /[\s,\(\[\{:=<>!+\-]/.test(code[i - 1]))) {
			let j = i;
			while (j < len && /[\d._xXa-fA-F]/.test(code[j])) j++;
			result.push(`<span class="hl-number">${code.slice(i, j)}</span>`);
			i = j;
			continue;
		}
		if (/[a-zA-Z_]/.test(code[i])) {
			let j = i;
			while (j < len && /[a-zA-Z0-9_]/.test(code[j])) j++;
			const word = code.slice(i, j);
			if (cfg.keywords.includes(word)) {
				result.push(`<span class="hl-keyword">${word}</span>`);
			} else {
				result.push(word);
			}
			i = j;
			continue;
		}
		result.push(code[i]);
		i++;
	}
	return result.join('');
}

function escapeHtml(text: string): string {
	return text
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

const renderer: RendererObject = {
	code({ text, lang }: Tokens.Code): string {
		const langLabel = lang ? `<span class="code-lang">${escapeHtml(lang)}</span>` : '';
		const highlighted = highlightCode(text, lang || '');
		// No inline onclick — output is sanitized (see renderMarkdown), which strips inline
		// handlers. The click is handled by the delegated listener in ensureClientSetup().
		const copyBtn = `<button class="copy-btn" type="button" aria-label="Copy code">Copy</button>`;
		const cls = lang ? ` class="language-${escapeHtml(lang)}"` : '';
		return `<div class="code-block">${langLabel}${copyBtn}<pre><code${cls}>${highlighted}</code></pre></div>`;
	},

	table({ header, rows }: Tokens.Table): string {
		const thead = '<thead><tr>' + header.map(cell =>
			`<th${cell.align ? ` style="text-align:${cell.align}"` : ''}>${this.parser.parseInline(cell.tokens)}</th>`
		).join('') + '</tr></thead>';

		const tbody = rows.length > 0
			? '<tbody>' + rows.map(row =>
				'<tr>' + row.map(cell =>
					`<td${cell.align ? ` style="text-align:${cell.align}"` : ''}>${this.parser.parseInline(cell.tokens)}</td>`
				).join('') + '</tr>'
			).join('') + '</tbody>'
			: '';

		return `<div class="table-wrap"><table>${thead}${tbody}</table></div>`;
	},

	link({ href, title, tokens }: Tokens.Link): string {
		const text = this.parser.parseInline(tokens);
		const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
		return `<a href="${escapeHtml(href)}"${titleAttr} target="_blank" rel="noopener">${text}</a>`;
	},
};

const marked = new Marked({
	renderer,
	gfm: true,
	breaks: true,
});

const _mdCache = new Map<string, string>();
const _MD_CACHE_MAX = 200;

let _clientReady = false;

/**
 * One-time, browser-only setup for the sanitized render path. Installs (1) a DOMPurify
 * hook that forces `rel="noopener noreferrer"` on any `target="_blank"` link (so a raw-HTML
 * link in untrusted source can't tabnab), and (2) a single delegated click listener that
 * drives the code-block "Copy" button — replacing the inline `onclick` that sanitization
 * strips. Delegation is one listener for all blocks, with no per-render binding cost.
 */
function ensureClientSetup(): void {
	if (_clientReady || typeof document === 'undefined') return;
	_clientReady = true;
	DOMPurify.addHook('afterSanitizeAttributes', (node) => {
		if (node.nodeName === 'A' && node.getAttribute('target') === '_blank') {
			node.setAttribute('rel', 'noopener noreferrer');
		}
	});
	document.addEventListener('click', (e) => {
		const btn = (e.target as Element | null)?.closest?.('.copy-btn');
		if (!btn) return;
		const code = btn.parentElement?.querySelector('code');
		if (!code) return;
		void navigator.clipboard.writeText(code.textContent ?? '').then(() => {
			btn.textContent = 'Copied!';
			setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
		});
	});
}

/**
 * Render markdown source to sanitized HTML. The output is the trust boundary for every
 * `{@html renderMarkdown(...)}` consumer (chat, tool-activity, conversation viewer, the
 * documents table widget): DOMPurify strips scripts, inline event handlers, and
 * `javascript:` URLs while preserving the renderer's structure (tables + alignment, code
 * blocks, highlight spans, links, the copy button). `target` is kept so external links
 * still open in a new tab.
 */
export function renderMarkdown(text: string): string {
	if (!text) return '';

	const cached = _mdCache.get(text);
	if (cached !== undefined) return cached; // already sanitized

	// Sanitization requires a DOM. Without one, DOMPurify silently returns the input
	// UNCHANGED (isSupported=false) — which would ship unsanitized HTML. The app is a SPA
	// (ssr=false), so this never runs server-side; fail loud rather than pass through, so the
	// "renderMarkdown always returns sanitized HTML" invariant can never break silently.
	if (typeof document === 'undefined') {
		throw new Error('renderMarkdown requires a DOM for sanitization — it must not run server-side');
	}

	ensureClientSetup();
	const html = marked.parse(text, { async: false }) as string;
	const result = DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });

	if (_mdCache.size >= _MD_CACHE_MAX) {
		const firstKey = _mdCache.keys().next().value;
		if (firstKey !== undefined) _mdCache.delete(firstKey);
	}
	_mdCache.set(text, result);
	return result;
}
