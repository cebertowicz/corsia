import { onDestroy, tick } from 'svelte';
import type { OperatorNode } from '$lib/api/plans';
import { apiFetch } from '$lib/api/request';
import { LONG_PRESS_MS } from '$lib/components/ui/rowMenu.svelte';
import { captureFold, playFold, prefersReducedMotion } from './foldWave';
import { planOrderGroup } from './planTreeFold';
import { positionBeside } from './planReorder';
import type { createTaskMenu } from './taskMenu.svelte';

type Draft = { id: string; targetId: string; position: number; before: boolean };
type Drag = { id: string; offsets: Record<string, number>; position: number; count: number };
type Press = { node: OperatorNode; pointerId: number; x: number; y: number; lastY: number; scroll: HTMLElement | null; armed: boolean; scrolling: boolean; timer: ReturnType<typeof setTimeout> | null };

interface Options {
	nodes: () => OperatorNode[];
	now: () => number;
	openBlocked: () => Set<string>;
	element: () => HTMLElement | null;
	planId: () => string;
	enabled: () => boolean;
	refresh: () => void;
	focusRow: (id: string) => boolean;
	menu: ReturnType<typeof createTaskMenu>;
}

function scrollParent(element: HTMLElement): HTMLElement | null {
	let parent = element.parentElement;
	while (parent && !(parent.scrollHeight > parent.clientHeight && /(auto|scroll)/.test(getComputedStyle(parent).overflowY))) parent = parent.parentElement;
	return parent;
}

/** Owns one tree's reorder gesture, optimistic order and mutation lifecycle. */
export function createPlanTreeReorder(options: Options) {
	let draft = $state<Draft | null>(null);
	let drag = $state<Drag | null>(null);
	let announcement = $state('');
	let error = $state<string | null>(null);
	let detail = $state<string | null>(null);
	let moving = $state(false);
	let suppressClick = false;
	let scrollFrame = 0;
	let dragCursorY = 0;
	let press: Press | null = null;
	const slop = 10;

	function orderedNodes(nodes: OperatorNode[], extra: OperatorNode[]): OperatorNode[] {
		const ordered = [...nodes, ...extra];
		if (!draft || drag) return ordered;
		const from = ordered.findIndex((node) => node.id === draft!.id);
		if (from >= 0 && ordered.some((node) => node.id === draft!.targetId)) {
			const [moved] = ordered.splice(from, 1);
			const to = ordered.findIndex((node) => node.id === draft!.targetId);
			ordered.splice(to + (draft.before ? 0 : 1), 0, moved);
		}
		return ordered;
	}
	function siblings(node: OperatorNode): OperatorNode[] {
		return options.nodes().filter((candidate) => candidate.parent_id === node.parent_id && (!candidate.folded || candidate.id === node.id));
	}
	function eligible(node: OperatorNode): boolean {
		return node.user_actions.includes('reorder') && planOrderGroup(node, options.now(), options.openBlocked()) !== null;
	}
	function hasMenu(node: OperatorNode): boolean {
		return node.user_actions.some((action) => action !== 'reorder') || eligible(node) || !!node.comment_attention;
	}
	function peers(node: OperatorNode): OperatorNode[] {
		const group = planOrderGroup(node, options.now(), options.openBlocked());
		return group ? siblings(node).filter((candidate) => planOrderGroup(candidate, options.now(), options.openBlocked()) === group) : [];
	}
	function availability(node: OperatorNode) {
		const group = peers(node);
		const at = group.findIndex((candidate) => candidate.id === node.id);
		return { eligible: eligible(node), up: at > 0, down: at >= 0 && at < group.length - 1 };
	}
	function clearDraftWithGlide() {
		const element = options.element();
		const snapshot = element && !prefersReducedMotion() ? captureFold(element, []) : null;
		draft = null;
		if (snapshot) void tick().then(() => { const tree = options.element(); if (tree) playFold(tree, snapshot); });
	}
	async function saveMove(node: OperatorNode, position: number): Promise<void> {
		moving = true; error = null; detail = null;
		try {
			const result = await apiFetch(`/api/plans/${encodeURIComponent(options.planId())}/mutations`, {
				method: 'POST', headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ operation: 'reorder', mutation_id: crypto.randomUUID(), params: { node_id: node.id, expected_revision: node.revision, order_key: position } }),
			});
			if (!result.ok) {
				const body = await result.json().catch(() => null);
				if (result.status === 409) { error = "Couldn't save the move because the plan had a newer change. The latest plan is showing; try again."; options.refresh(); }
				else { error = "Couldn't move this task. Try again."; detail = typeof body?.detail === 'string' ? body.detail : `Request failed (${result.status}).`; }
				await tick(); clearDraftWithGlide(); announcement = error;
			} else options.refresh();
		} catch (cause) {
			error = "Couldn't move this task. Try again.";
			detail = cause instanceof Error ? cause.message : 'The request could not be completed.';
			await tick(); clearDraftWithGlide(); announcement = error;
		} finally {
			moving = false; await tick(); options.focusRow(node.id);
		}
	}
	async function moveRow(node: OperatorNode, direction: 'up' | 'down' | 'top'): Promise<string | null> {
		const group = peers(node);
		const current = group.findIndex((candidate) => candidate.id === node.id);
		const target = direction === 'top' ? 0 : current + (direction === 'up' ? -1 : 1);
		if (!eligible(node) || moving || current < 0 || target < 0 || target >= group.length || target === current) return null;
		const before = direction !== 'down';
		const position = positionBeside(siblings(node), node.id, group[target].id, before);
		if (position === null) return null;
		draft = { id: node.id, targetId: group[target].id, position, before };
		await tick(); await saveMove(node, position);
		if (!error) announcement = `${node.title} moved to ${target + 1} of ${group.length}`;
		return null;
	}
	function scrollWhileDragging() {
		const scroller = press?.scroll;
		if (!drag || !scroller) return;
		const box = scroller.getBoundingClientRect();
		const edge = parseFloat(getComputedStyle(scroller).getPropertyValue('--space-6')) || box.height / 20;
		if (dragCursorY < box.top + edge) scroller.scrollTop -= edge / 4;
		if (dragCursorY > box.bottom - edge) scroller.scrollTop += edge / 4;
		scrollFrame = requestAnimationFrame(scrollWhileDragging);
	}
	onDestroy(() => { if (press?.timer) clearTimeout(press.timer); cancelAnimationFrame(scrollFrame); });
	$effect(() => {
		if (!draft || drag || moving) return;
		const node = options.nodes().find((candidate) => candidate.id === draft!.id);
		if (!node || siblings(node).findIndex((candidate) => candidate.id === node.id) === draft.position) draft = null;
	});
	function updateDrag(node: OperatorNode, y: number) {
		const group = peers(node);
		const slots = group.map((candidate) => ({ node: candidate, element: options.element()?.querySelector<HTMLElement>(`[data-node-id="${CSS.escape(candidate.id)}"]`) }))
			.filter((entry): entry is { node: OperatorNode; element: HTMLElement } => !!entry.element)
			.map(({ node: candidate, element }) => { const box = element.getBoundingClientRect(); return { id: candidate.id, top: box.top - new DOMMatrixReadOnly(getComputedStyle(element).transform).m42, height: box.height }; }).sort((a, b) => a.top - b.top);
		const nearest = slots.reduce<typeof slots[number] | null>((best, slot) => !best || Math.abs(y - (slot.top + slot.height / 2)) < Math.abs(y - (best.top + best.height / 2)) ? slot : best, null);
		const from = slots.findIndex((slot) => slot.id === node.id);
		const to = slots.findIndex((slot) => slot.id === nearest?.id);
		if (from < 0 || to < 0 || !nearest) return;
		const before = y < nearest.top + nearest.height / 2;
		const position = positionBeside(siblings(node), node.id, nearest.id, before);
		const visualPosition = from === to ? from : to - (from < to ? 1 : 0) + (before ? 0 : 1);
		if (position === null || visualPosition === from) { draft = null; drag = { id: node.id, offsets: {}, position: from, count: group.length }; return; }
		draft = { id: node.id, targetId: nearest.id, position, before };
		const offsets: Record<string, number> = {};
		const source = slots[from];
		offsets[node.id] = visualPosition < from ? slots[visualPosition].top - source.top : slots[visualPosition].top + slots[visualPosition].height - source.top - source.height;
		for (let i = Math.min(from, visualPosition); i <= Math.max(from, visualPosition); i++) if (i !== from) offsets[slots[i].id] = visualPosition < from ? source.height : -source.height;
		drag = { id: node.id, offsets, position: visualPosition, count: group.length };
	}
	function rowHandlers(node: OperatorNode) {
		const menu = options.menu.rowHandlers(node);
		return {
			...menu,
			onpointerdown(event: PointerEvent) {
				suppressClick = false;
				if (event.pointerType !== 'touch') menu.onpointerdown(event);
				const movable = options.enabled() && eligible(node) && !moving;
				const touchMenu = event.pointerType === 'touch' && options.enabled() && hasMenu(node);
				if (event.button !== 0 || (!movable && !touchMenu)) return;
				const row = event.currentTarget as HTMLElement;
				press = { node, pointerId: event.pointerId, x: event.clientX, y: event.clientY, lastY: event.clientY, scroll: scrollParent(row), armed: event.pointerType !== 'touch', scrolling: false, timer: null };
				if (event.pointerType === 'touch') press.timer = setTimeout(() => { if (press?.pointerId === event.pointerId && !press.scrolling) press.armed = true; }, LONG_PRESS_MS);
				(event.target as HTMLElement).setPointerCapture?.(event.pointerId);
			},
			onpointermove(event: PointerEvent) {
				const current = press;
				if (!current || current.pointerId !== event.pointerId) return;
				const distance = Math.hypot(event.clientX - current.x, event.clientY - current.y);
				if (!current.armed) {
					if (distance > slop) { current.scrolling = true; suppressClick = true; if (current.timer) clearTimeout(current.timer); }
					if (current.scrolling && current.scroll) current.scroll.scrollTop += current.lastY - event.clientY;
					current.lastY = event.clientY; return;
				}
				if (distance <= slop && !drag) return;
				if (!eligible(node) || !options.enabled() || moving) { current.scrolling = true; return; }
				if (!drag) { options.menu.close(); error = null; suppressClick = true; announcement = `Moving ${node.title}`; scrollFrame = requestAnimationFrame(scrollWhileDragging); }
				dragCursorY = event.clientY;
				updateDrag(node, event.clientY);
			},
			async onpointerup(event: PointerEvent) {
				menu.onpointerup();
				const current = press;
				if (!current || current.pointerId !== event.pointerId) return;
				if (current.timer) clearTimeout(current.timer);
				press = null; cancelAnimationFrame(scrollFrame);
				if (event.pointerType === 'touch' && current.armed && !current.scrolling && !drag) { suppressClick = true; options.menu.openFor(node, event.currentTarget as HTMLElement, true); return; }
				if (!drag) return;
				const next = draft, count = drag.count, visualPosition = drag.position;
				drag = null;
				if (!next || next.id === next.targetId) { draft = null; return; }
				await tick(); await saveMove(current.node, next.position);
				if (!error) announcement = `${node.title} moved to ${visualPosition + 1} of ${count}`;
			},
			onpointercancel() { menu.onpointercancel(); if (press?.timer) clearTimeout(press.timer); cancelAnimationFrame(scrollFrame); press = null; drag = null; draft = null; },
		};
	}
	return {
		get draft() { return draft; }, get drag() { return drag; }, get announcement() { return announcement; },
		get error() { return error; }, get detail() { return detail; },
		hasMenu, eligible, availability, moveRow, rowHandlers, orderedNodes,
		consumeClick() { if (!suppressClick) return false; suppressClick = false; return true; },
		dismissError() { error = null; detail = null; },
	};
}
