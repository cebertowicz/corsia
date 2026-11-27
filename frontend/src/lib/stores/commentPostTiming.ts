// Where a new comment's post spends its time, from the press of Comment to the card on screen, for the server's log.
// It carries numbers only: no comment text and no ids. The parts known before the POST ride on the POST itself in
// `COMMENT_TIMING_HEADER`; the parts known after it ride on this tab's next comment request, GET or POST. A post is
// keyed by its draft's client id while it runs; the header never carries that id, only this tab's count of posts.

import { CaptureFailed } from './captureFailed';

export const COMMENT_TIMING_HEADER = 'X-Corsia-Comment-Timing';

export type PostOutcome = 'delivered' | 'failed' | `failed:${string}` | 'timeout' | 'earlier';
type Part = 'cap' | 'ens' | 'post' | 'rec';
type Running = { n: number; a: number; pressed: number } & Partial<Record<Part | 'app', number>>;

let posts = 0;
const running = new Map<string, Running>();
/** Presses of Comment per draft id, until that draft's comment shows. */
const attempts = new Map<string, number>();
/** The last finished post's after-POST parts, until a comment request carrying them gets a successful answer. A post
 * that finishes later replaces them: the last one wins. */
let report: string | null = null;

const ms = (value: number) => Math.max(0, Math.round(value));

/** Comment was pressed on the draft whose id is `clientId`. */
export function pressed(clientId: string): void {
	const a = (attempts.get(clientId) ?? 0) + 1;
	attempts.set(clientId, a);
	running.set(clientId, { n: ++posts, a, pressed: performance.now() });
}

/** A press of Comment on the draft whose id is `clientId` is being posted now. */
export function posting(clientId: string): boolean { return running.has(clientId); }

/** Runs one awaited part of a running post and records how long it took, whether it resolved or failed. */
export async function timed<T>(clientId: string | undefined, part: Part, run: () => Promise<T>): Promise<T> {
	const started = performance.now();
	try {
		return await run();
	} finally {
		const post = clientId ? running.get(clientId) : undefined;
		if (post) post[part] = ms(performance.now() - started);
	}
}

/** The server's own time for the POST, from its `Server-Timing` answer (`app;dur=`). */
export function noteServerTiming(clientId: string | undefined, serverTiming: string | null): void {
	const post = clientId ? running.get(clientId) : undefined;
	const app = serverTiming && /(?:^|,)\s*app;dur=([\d.]+)/.exec(serverTiming);
	if (post && app) post.app = ms(Number(app[1]));
}

/** The post ended. Its after-POST parts wait for this tab's next comment request. */
export function finished(clientId: string, outcome: PostOutcome): void {
	const post = running.get(clientId);
	if (!post) return;
	running.delete(clientId);
	if (outcome === 'delivered' || outcome === 'earlier') attempts.delete(clientId);
	const parts = [`pn=${post.n}`, `rec=${post.rec === undefined ? 0 : 1}`, `out=${outcome}`, `total=${ms(performance.now() - post.pressed)}`];
	if (post.post !== undefined) parts.push(`post=${post.post}`);
	if (post.app !== undefined) parts.push(`app=${post.app}`);
	if (post.rec !== undefined) parts.push(`recms=${post.rec}`);
	report = parts.join(',');
}

/** What a refused post reports: `failed:<status>` and, when the refusal's body carries its own short code, `:<code>`.
 * A live capture that ended without a place reports `failed:cap:<code>`, its way of ending (`CaptureCode`).
 * Never the refusal's sentence, so no path or quote reaches the log; a failure with no status (a lost connection) is plain `failed`. */
function failedOutcome(error: unknown): PostOutcome {
	if (error instanceof CaptureFailed) return `failed:cap:${error.code}`;
	const wire = error instanceof Error ? /^(\d{3})\s*([\s\S]*)$/.exec(error.message) : null;
	if (!wire) return 'failed';
	let kind: unknown;
	try { kind = JSON.parse(wire[2])?.kind; } catch { /* A body that is not JSON has no code. */ }
	// {0,31} is _CODE_CHARS (32) in backend/api/comment_post_timing.py, where its basis is stated.
	return typeof kind === 'string' && /^[a-z][a-z_]{0,31}$/.test(kind) ? `failed:${wire[1]}:${kind}` : `failed:${wire[1]}`;
}

/** One press of Comment, timed from the press to its comment on screen (`run` resolving) or its failure. */
export async function timedPost<T>(clientId: string, run: () => Promise<T>): Promise<T> {
	pressed(clientId);
	try {
		const result = await run();
		finished(clientId, 'delivered');
		return result;
	} catch (error) {
		const name = error instanceof Error ? error.name : '';
		finished(clientId, name === 'EarlierVersionPosted' ? 'earlier' : name === 'PostTimeoutError' ? 'timeout' : failedOutcome(error));
		throw error;
	}
}

/** A Cancel: the draft's presses are forgotten with it. */
export function forgetDraft(clientId: string | undefined): void {
	if (clientId) attempts.delete(clientId);
}

/**
 * The header a comment request carries, if any: a POST's own before-POST parts, and the last finished post's report.
 * The report stays pending until `answered` sees this request succeed, so a failed request (a post that failed, then a
 * close, is the case it matters for) leaves it for the next one. A report that replaced it meanwhile stays.
 */
export function timingHeaders(clientId?: string): { headers: Record<string, string>; answered: (response: Response) => void } {
	const post = clientId ? running.get(clientId) : undefined;
	const parts = post ? [`n=${post.n}`, `a=${post.a}`] : [];
	if (post?.cap !== undefined) parts.push(`cap=${post.cap}`);
	if (post?.ens !== undefined) parts.push(`ens=${post.ens}`);
	const carried = report;
	if (carried) parts.push(carried);
	return {
		headers: parts.length ? { [COMMENT_TIMING_HEADER]: parts.join(',') } : {},
		answered: (response) => { if (response.ok && carried !== null && report === carried) report = null; }
	};
}

/** Tests only: a fresh tab. */
export function resetCommentPostTiming(): void {
	posts = 0; running.clear(); attempts.clear(); report = null;
}
