<script lang="ts">
	import { onMount } from 'svelte';
	import { placeHighlightDot, proto } from '$lib/components/comments/highlightDot';
	import '$lib/components/commentAnchors.css';

	// PROTOTYPE (uncommitted): a chat transcript with unread comment threads, drawn with the real new-sign code.
	type Case = { id: string; title: string; text: string; marks: { quote: string; at?: 'edge'; read?: true }[] };
	const cases: Case[] = [
		{ id: 'word-first', title: 'One word, first line', text: 'Priya Raman signed off on the Lisbon vendor budget this morning, and Marcus Chen has the migration checklist ready for a second pass. I will hold the contract summary for Dana Okafor until legal confirms the renewal date on Friday afternoon.', marks: [{ quote: 'Priya' }] },
		{ id: 'word-mid', title: 'One word, mid-paragraph', text: 'The staging rollout for the Lisbon office is scheduled for Thursday evening once the network team finishes its checks. Marcus Chen owns the cutover plan, and the rollback steps are written down in the shared runbook. Dana Okafor still needs the final headcount.', marks: [{ quote: 'rollback' }] },
		{ id: 'long-first', title: 'Long highlight, from the first line', text: 'Move the vendor review into the shared planning document and flag the open budget line for Priya Raman before the Thursday sync, so nobody has to ask which item is still unresolved. The checklist can stay where it is for now.', marks: [{ quote: 'Move the vendor review into the shared planning document and flag the open budget line for Priya Raman' }] },
		{ id: 'long-mid', title: 'Long highlight, mid-paragraph', text: 'Here is where things stand after the review. The migration checklist still needs a second pass from Marcus Chen, and the Lisbon staging rollout depends on it finishing before Friday, which leaves very little room if legal replies late. I will keep watching the thread.', marks: [{ quote: 'migration checklist still needs a second pass from Marcus Chen, and the Lisbon staging rollout depends on it finishing before Friday' }] },
		{ id: 'edge', title: 'Highlight at the right edge of a line', text: 'The renewal summary for Dana Okafor covers three vendors and one open contract. Each line item links back to the shared planning document so the numbers can be checked against the original quotes from the Lisbon team without anyone digging through email.', marks: [{ quote: '', at: 'edge' }] },
		{ id: 'end', title: 'Highlight ends its paragraph', text: 'Dana Okafor still needs the headcount for the Lisbon office, and Marcus Chen is waiting on the final budget line.', marks: [{ quote: 'final budget line.' }] },
		{ id: 'adjacent', title: 'Two unread threads on adjacent lines', text: 'Before Friday we need three things settled: the contract renewal date from legal, the final headcount for Lisbon, and a second pass on the migration checklist from Marcus Chen. Everything else in the plan can slip a day without anyone noticing.', marks: [{ quote: 'contract renewal date' }, { quote: 'second pass on the migration' }] },
		{ id: 'mixed', title: 'One read highlight and one unread, same paragraph', text: 'Marcus Chen confirmed the rollback steps with the network team on Tuesday, so the only open item is the contract renewal date, which legal has promised for Friday morning at the latest.', marks: [{ quote: 'rollback steps', read: true }, { quote: 'contract renewal date' }] },
		{ id: 'same-end', title: 'Two unread threads ending at the same offset', text: 'Legal still has to confirm the contract renewal date before Dana Okafor signs anything, and she will not move until it arrives on Friday afternoon.', marks: [{ quote: 'confirm the contract renewal date' }, { quote: 'renewal date' }] },
		{ id: 'same-word', title: 'Two unread threads on the same word', text: 'The vendor list for Lisbon still needs a final headcount from Marcus Chen, and the budget line cannot close until that number is in.', marks: [{ quote: 'headcount' }, { quote: 'headcount' }] },
	];
	const params = new URLSearchParams(location.search);
	const variant = (params.get('variant') ?? 'before') as typeof proto.variant;
	const theme = params.get('theme') ?? 'light';
	const rail = params.get('rail') === '1';
	proto.variant = variant;
	document.documentElement.setAttribute('data-theme', theme);

	let scroller: HTMLElement;
	const sidebar = innerWidth > 1024 && !rail ? 280 : 0;

	function wrap(body: HTMLElement, from: number, to: number, id: string) {
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		let offset = 0, node: Node | null;
		const range = document.createRange();
		let started = false;
		while ((node = walker.nextNode())) {
			const len = (node.textContent ?? '').length;
			if (!started && from < offset + len) { range.setStart(node, from - offset); started = true; }
			if (started && to <= offset + len) { range.setEnd(node, to - offset); break; }
			offset += len;
		}
		const span = document.createElement('span');
		span.className = 'cm-comment-wrap cm-comment-anchor'; span.dataset.commentIds = id;
		range.surroundContents(span);
		return span;
	}

	// The last word that ends a wrapped line past the first: the word whose right edge is nearest the column's right edge.
	function edgeWord(body: HTMLElement) {
		const text = body.textContent ?? '';
		const bodyRect = body.getBoundingClientRect();
		const node = body.querySelector('p')!.firstChild!;
		const base = text.indexOf((node.textContent ?? '').slice(0, 12));
		let best = { from: 0, to: 0, score: 1e9 };
		const re = /\S+/g; let m: RegExpExecArray | null;
		while ((m = re.exec(node.textContent ?? ''))) {
			const r = document.createRange(); r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
			const rects = [...r.getClientRects()];
			const line = Math.round((rects[0].top - bodyRect.top) / 25);
			if (line < 1 || m[0].length < 4 || rects.length > 1) continue;
			const score = bodyRect.right - rects[0].right;
			if (score >= 0 && score < best.score) best = { from: base + m.index, to: base + m.index + m[0].length, score };
		}
		return best;
	}

	onMount(() => {
		const cleanups: (() => void)[] = [];
		document.fonts.ready.then(() => requestAnimationFrame(() => {
			scroller.querySelectorAll<HTMLElement>('[data-case]').forEach((row) => {
				const c = cases.find((x) => x.id === row.dataset.case)!;
				const body = row.querySelector<HTMLElement>('.md-content')!;
				c.marks.forEach((mk, i) => {
					const text = body.textContent ?? '';
					let from: number, to: number;
					if (mk.at === 'edge') { const e = edgeWord(body); from = e.from; to = e.to; }
					else { from = text.indexOf(mk.quote); to = from + mk.quote.length; }
					const span = wrap(body, from, to, `${c.id}-${i}`);
					if (mk.read) span.dataset.read = '1';
					if (!mk.read) cleanups.push(placeHighlightDot(body, span, null, undefined, 'New reply'));
				});
			});
			document.documentElement.dataset.ready = '1';
		}));
		return () => cleanups.forEach((fn) => fn());
	});
</script>

<div class="app" style:--sidebar={`${sidebar}px`}>
	{#if sidebar}<aside class="sidebar" aria-hidden="true"></aside>{/if}
	<main class="messages" bind:this={scroller}>
		<div class="transcript">
			<div class="message assistant"><div class="md-content"><p>Notes saved to the shared planning document.</p></div><div class="msg-footer"><span class="msg-time">4:41 PM</span></div></div>
			{#each cases as c (c.id)}
				<div class="message assistant" data-case={c.id}>
					<div class="case-title">{c.title}</div>
					<div class="comment-mark-layer"><div class="md-content long-message-body"><p>{c.text}</p></div></div>
					<div class="msg-footer"><span class="msg-time">4:49 PM</span></div>
				</div>
			{/each}
		</div>
	</main>
</div>

<style>
	.app { display: flex; height: 100vh; background: var(--bg-primary); color: var(--text-primary); }
	.sidebar { width: var(--sidebar); flex: none; }
	.messages { flex: 1; min-width: 0; overflow-y: auto; padding: 24px 16px 40px; }
	@media (max-width: 768px) { .messages { padding-left: 12px; padding-right: 12px; } }
	.transcript { max-width: var(--transcript-max-width); margin: 0 auto; }
	.message { margin: 0 0 var(--space-4); }
	.case-title { display: none; }
	.msg-footer { margin-top: 4px; }
	.msg-time { font-size: var(--text-caption); color: var(--text-secondary); opacity: 0.7; }
	.comment-mark-layer { position: relative; }
</style>
