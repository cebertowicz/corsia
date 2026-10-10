/**
 * A plan's folded history, on demand (`backend/plans/folded_reads.py`). A pane
 * read lists the open work and a window of folded rows; the rest of a level's
 * finished work is paged here, newest first, and one row's place is found by
 * its path. Same read boundary as the operator envelope (`planRead`).
 */
import { readPlan, type ReadOutcome } from './planRead';
import type { OperatorDisplay, OperatorNodeLabelKey } from './plansTypes';

/**
 * One level's folded work, as the server counts it (`plans.open_folds`): the
 * folded rows directly beneath `parent_id` (null: the plan's top level) by the
 * tone each row shows, and how many of them this read did not list in `nodes`.
 * A level with nothing folded has no entry. The client computes no fold count.
 */
export interface OperatorFoldLevel {
	parent_id: string | null;
	done: number;
	failed: number;
	closed: number;
	more: number;
}

/** One folded row as the history route sends it: enough to draw the row and to page the level beneath it. */
export interface FoldedItem {
	id: string;
	parent_id: string | null;
	kind: string;
	title: string;
	state: string;
	folded_at: string;
	fold_bucket: string | null;
	display: OperatorDisplay<OperatorNodeLabelKey>;
	/** The folded rows directly beneath this one, by tone. */
	folds: { done: number; failed: number; closed: number };
	has_folded_children: boolean;
}

export interface FoldedPage {
	plan_id: string;
	/** The level paged: a node id, or `root`. */
	parent: string;
	items: FoldedItem[];
	/** Pass back as `cursor` for the next page; null on the last one. */
	next_cursor: string | null;
}

export interface FoldedPath {
	plan_id: string;
	/** The node as a history row; null when it is not folded (it sits in the open work). */
	node: FoldedItem | null;
	/** Root first. */
	ancestors: { id: string; kind: string; title: string; state: string; folded: boolean }[];
	page: { parent: string; index: number; page_index: number; page_size: number; cursor: string | null } | null;
}

export type FoldedReadOutcome<T> = Exclude<ReadOutcome<T>, { status: 'not-modified' }>;

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === 'string';
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isStrOrNull = (v: unknown): v is string | null => v === null || isStr(v);

function isItem(v: unknown): v is FoldedItem {
	return isObject(v) && isStr(v.id) && isStrOrNull(v.parent_id) && isStr(v.kind) && isStr(v.title)
		&& isStr(v.state) && isStr(v.folded_at) && isStrOrNull(v.fold_bucket)
		&& isObject(v.display) && isStr(v.display.label_key) && isStr(v.display.tone_key)
		&& isObject(v.folds) && isNum(v.folds.done) && isNum(v.folds.failed) && isNum(v.folds.closed)
		&& typeof v.has_folded_children === 'boolean';
}

export function decodeFoldedPage(v: unknown): FoldedPage | null {
	if (!isObject(v) || !isStr(v.plan_id) || !isStr(v.parent) || !isStrOrNull(v.next_cursor)) return null;
	if (!Array.isArray(v.items) || !v.items.every(isItem)) return null;
	return v as unknown as FoldedPage;
}

export function decodeFoldedPath(v: unknown): FoldedPath | null {
	if (!isObject(v) || !isStr(v.plan_id) || !(v.node === null || isItem(v.node))) return null;
	const ancestor = (a: unknown) => isObject(a) && isStr(a.id) && isStr(a.kind) && isStr(a.title)
		&& isStr(a.state) && typeof a.folded === 'boolean';
	if (!Array.isArray(v.ancestors) || !v.ancestors.every(ancestor)) return null;
	const page = v.page;
	if (page !== null && !(isObject(page) && isStr(page.parent) && isNum(page.index) && isNum(page.page_index)
		&& isNum(page.page_size) && isStrOrNull(page.cursor))) return null;
	return v as unknown as FoldedPath;
}

/** The level a history read pages when it is the plan's top level. */
export const FOLDED_ROOT = 'root';

/** One page of the folded rows directly beneath `parent` (a node id or `root`), after `cursor`. */
export async function readFoldedPage(
	bearerValue: string, planId: string, parent: string, cursor: string | null,
): Promise<FoldedReadOutcome<FoldedPage>> {
	const query = new URLSearchParams({ parent });
	if (cursor) query.set('cursor', cursor);
	const outcome = await readPlan(
		`/api/plans/${encodeURIComponent(planId)}/folded?${query}`, bearerValue, null, decodeFoldedPage,
	);
	return outcome as FoldedReadOutcome<FoldedPage>;
}

/** A node's ancestors and, when folded, where it sits among its level's folded rows. */
export async function readFoldedPath(
	bearerValue: string, planId: string, nodeId: string,
): Promise<FoldedReadOutcome<FoldedPath>> {
	const outcome = await readPlan(
		`/api/plans/${encodeURIComponent(planId)}/nodes/${encodeURIComponent(nodeId)}/path`,
		bearerValue, null, decodeFoldedPath,
	);
	return outcome as FoldedReadOutcome<FoldedPath>;
}
