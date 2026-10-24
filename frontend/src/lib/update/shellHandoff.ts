/**
 * The update screen's move between the local-service page and the desktop shell's own update page.
 *
 * Only the shell's page may ask the shell to bring the service back, so a local page that reaches that point hands
 * its screen over, and the shell hands it back once the service is ready. What travels in the URL fragment is the
 * screen's presentation: its progress model, stall clocks, versions, theme and the page to return to. It grants
 * nothing: the shell decides on its own whether a start is allowed, and nothing in it selects an action.
 */

import type { UpdateFailureCode } from './model';
import type { BandMode, ProgressSnapshot, StepId } from './screenProgress';

export const HANDOFF_KEY = 'corsia-update';
/** The longest fragment either page reads; anything longer is ignored. */
export const HANDOFF_MAX_CHARS = 2_048;
/** The shell's update page. It is bundled with the app and drawn by nothing else. */
export const SHELL_UPDATE_PAGE = 'tauri://localhost/update.html';
/** Clocks further than this from now are not this update's. */
const CLOCK_SLACK_MS = 24 * 60 * 60 * 1_000;

export interface ScreenHandoff {
	v: 1;
	operationId: string;
	model: ProgressSnapshot;
	fromVersion: string | null;
	toVersion: string | null;
	usualSeconds: number | null;
	downAt: number | null;
	seqChangedAt: number | null;
	lastSeq: number | null;
	lastPid: number | null;
	failure: { code: UpdateFailureCode | null; verdict: boolean } | null;
	theme: 'light' | 'dark';
	route: string;
}

const STEPS: readonly StepId[] = ['waiting', 'restarting', 'backing_up', 'upgrading', 'checking_data', 'starting', 'verifying'];
const MODES: readonly BandMode[] = ['migration', 'plain'];
const CODES: readonly UpdateFailureCode[] = [
	'disk_space', 'fetch_failed', 'build_failed', 'launch_failed', 'rolled_back', 'version_unverified', 'wrong_version_running',
	'drain_cancelled', 'drain_failed', 'update_timed_out', 'prepare_failed', 'index_locked', 'checkout_detached', 'checkout_dirty',
	'restart_pending',
];

const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const percent = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
const whole = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const clock = (value: unknown, now: number): value is number | null => value === null || (typeof value === 'number' && Number.isFinite(value) && Math.abs(value - now) <= CLOCK_SLACK_MS);
const version = (value: unknown): value is string | null => value === null || (typeof value === 'string' && /^[\w.+-]{1,64}$/.test(value));

/** A same-origin path to come back to, or null. It never carries a scheme, host, fragment or backslash. */
export function safeReturnRoute(value: unknown): string | null {
	if (typeof value !== 'string' || value.length > 512 || !value.startsWith('/') || value.startsWith('//') || /[\\\s]/.test(value)) return null;
	try {
		const base = 'http://route.invalid';
		const url = new URL(value, base);
		if (url.origin !== base) return null;
		return `${url.pathname}${url.search}`;
	} catch {
		return null;
	}
}

function parseModel(raw: unknown, now: number): ProgressSnapshot | null {
	if (!isRecord(raw)) return null;
	const { value, step, mode, lifted, visitedMigration, lastAt } = raw;
	if (!percent(value) || !percent(lifted) || typeof visitedMigration !== 'boolean' || !clock(lastAt, now)) return null;
	if (!STEPS.includes(step as StepId) || !MODES.includes(mode as BandMode)) return null;
	return { value, step: step as StepId, mode: mode as BandMode, lifted, visitedMigration, lastAt };
}

function parseFailure(raw: unknown): ScreenHandoff['failure'] | undefined {
	if (raw === null) return null;
	if (!isRecord(raw) || typeof raw.verdict !== 'boolean') return undefined;
	if (raw.code !== null && !CODES.includes(raw.code as UpdateFailureCode)) return undefined;
	return { code: raw.code as UpdateFailureCode | null, verdict: raw.verdict };
}

/** Only a whole, current handoff counts; anything else is no handoff at all. */
export function parseHandoff(raw: unknown, now = Date.now()): ScreenHandoff | null {
	if (!isRecord(raw) || raw.v !== 1) return null;
	const model = parseModel(raw.model, now);
	const failure = parseFailure(raw.failure);
	const route = safeReturnRoute(raw.route);
	const { operationId, fromVersion, toVersion, usualSeconds, downAt, seqChangedAt, lastSeq, lastPid, theme } = raw;
	if (!model || failure === undefined || route === null) return null;
	if (typeof operationId !== 'string' || !/^[\w-]{1,128}$/.test(operationId)) return null;
	if (!version(fromVersion) || !version(toVersion)) return null;
	if (usualSeconds !== null && !(typeof usualSeconds === 'number' && Number.isFinite(usualSeconds) && usualSeconds > 0 && usualSeconds <= 86_400)) return null;
	if (!clock(downAt, now) || !clock(seqChangedAt, now)) return null;
	if ((lastSeq !== null && !whole(lastSeq)) || (lastPid !== null && !whole(lastPid))) return null;
	if (theme !== 'light' && theme !== 'dark') return null;
	return { v: 1, operationId, model, fromVersion, toVersion, usualSeconds, downAt, seqChangedAt, lastSeq, lastPid, failure, theme, route };
}

export function encodeHandoff(handoff: ScreenHandoff): string {
	return `${HANDOFF_KEY}=${encodeURIComponent(JSON.stringify(handoff))}`;
}

/** Reads a handoff from a URL fragment (with or without its `#`). */
export function readHandoff(hash: string, now = Date.now()): ScreenHandoff | null {
	const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
	if (fragment.length > HANDOFF_MAX_CHARS || !fragment.startsWith(`${HANDOFF_KEY}=`)) return null;
	try {
		return parseHandoff(JSON.parse(decodeURIComponent(fragment.slice(HANDOFF_KEY.length + 1))), now);
	} catch {
		return null;
	}
}

/** Takes the handoff out of this page's URL, so a reload or a shared link never replays it. */
export function takeHandoffFromLocation(): ScreenHandoff | null {
	if (typeof window === 'undefined' || !window.location.hash.slice(1).startsWith(`${HANDOFF_KEY}=`)) return null;
	const handoff = readHandoff(window.location.hash);
	history.replaceState(history.state, '', `${window.location.pathname}${window.location.search}`);
	return handoff;
}

/**
 * Keeps the screen in this page's own address, or takes it out, leaving its path and history entry as they are. The
 * local-service page does this while its screen shows, so an app the update reopens carries it (`relaunch_arguments`).
 */
export function keepScreenInAddress(screen: ScreenHandoff | null): void {
	if (typeof window === 'undefined') return;
	const kept = window.location.hash.slice(1).startsWith(`${HANDOFF_KEY}=`);
	if (!screen && !kept) return;
	const page = `${window.location.pathname}${window.location.search}`;
	history.replaceState(history.state, '', screen ? `${page}#${encodeHandoff(screen)}` : page);
}

/** The kept page to restore when a handed-back screen arrived on another one (a reopened app lands on `/`), or null. */
export function routeToRestore(route: string, pathname: string, search: string): string | null {
	return route === `${pathname}${search}` ? null : route;
}

let returnRoute = '/';

/** The shell's update page keeps the page it came from, to send the window back there. */
export function keepReturnRoute(route: string): void {
	returnRoute = safeReturnRoute(route) ?? '/';
}

export function keptReturnRoute(): string {
	return returnRoute;
}

export function currentTheme(): 'light' | 'dark' {
	return typeof document !== 'undefined' && document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

/** Moves the window, replacing this page in its history so Back never returns to a screen that moved on. */
export function moveWindow(url: string): void {
	window.location.replace(url);
}

/** Where the shell sends the window back to once the service is ready: the native URL's origin and the kept page. */
export function returnUrl(nativeUrl: string, handoff: ScreenHandoff): string | null {
	try {
		const url = new URL(nativeUrl);
		if (url.protocol !== 'http:' || (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost')) return null;
		return `${url.origin}${handoff.route}#${encodeHandoff(handoff)}`;
	} catch {
		return null;
	}
}
