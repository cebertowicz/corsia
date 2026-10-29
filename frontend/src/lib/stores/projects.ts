import { writable, get } from 'svelte/store';
import { token } from './auth';
import { on } from '$lib/ws/events';
import { plainResponseError } from '../plainError';
import { enterScope, exitScope } from './uiScope';
import { projectAccent } from '$lib/utils/projectColors';
import type { Thread } from './threads';

export interface ProjectInfo {
	id: string;
	name: string;
	status: string;
	accent_color: string | null;
}

/** The minimal project shape UI surfaces hand back when the user picks a project. */
export interface ProjectPick {
	id: string;
	name: string;
	accent_color?: string | null;
}

export const projects = writable<ProjectInfo[]>([]);
export type ProjectLoadStatus = 'idle' | 'loading' | 'loaded' | 'error';
export const projectLoadStatus = writable<ProjectLoadStatus>('idle');
export const projectLoadError = writable<string | null>(null);

let loadedToken: string | null = null;
let inFlight: Promise<void> | null = null;
let reloadAfterFlight = false;
let watchingChanges = false;
let generation = 0;
let observedToken: string | null | undefined;

/** One auth-epoch-owned catalog request; an empty response is terminally loaded. */
export function loadProjects({ force = false }: { force?: boolean } = {}): Promise<void> {
	const currentToken = get(token);
	if (!currentToken) return Promise.resolve();
	watchChanges();
	if (!force && loadedToken === currentToken && get(projectLoadStatus) === 'loaded') {
		return Promise.resolve();
	}
	if (inFlight !== null) {
		// A forced read asked for during a read that began earlier must not take that read's answer.
		if (force) reloadAfterFlight = true;
		return inFlight;
	}

	projectLoadStatus.set('loading');
	projectLoadError.set(null);
	const epoch = currentToken;
	const requestGeneration = generation;
	inFlight = fetch(`/api/projects?token=${encodeURIComponent(epoch)}&status=all`)
		.then(async (res) => {
			if (!res.ok) throw new Error('Could not load projects');
			const data = await res.json();
			if (get(token) !== epoch || generation !== requestGeneration) return;
			projects.set((data.projects || []).map(toProjectInfo));
			loadedToken = epoch;
			projectLoadStatus.set('loaded');
			projectLoadError.set(null);
		})
		.catch(() => {
			if (get(token) !== epoch || generation !== requestGeneration) return;
			projectLoadStatus.set('error');
			projectLoadError.set('Could not load projects');
		})
		.finally(() => {
			if (get(token) !== epoch || generation !== requestGeneration) return;
			inFlight = null;
			if (reloadAfterFlight) {
				reloadAfterFlight = false;
				void loadProjects({ force: true });
			}
		});
	return inFlight;
}

/** Invalidate every representation owned by this catalog without leaking late responses. */
function resetCatalog(): void {
	generation++;
	loadedToken = null;
	inFlight = null;
	reloadAfterFlight = false;
	projects.set([]);
	projectLoadStatus.set('idle');
	projectLoadError.set(null);
}

/**
 * A project created outside this tab's own forms (a conductor's `project_create`,
 * another tab) is announced with `projects_changed`; a tab that has asked for
 * the catalog re-reads it on that nudge.
 */
function watchChanges(): void {
	if (watchingChanges) return;
	watchingChanges = true;
	on('projects_changed', () => {
		void loadProjects({ force: true });
	});
}

token.subscribe((next) => {
	if (next === observedToken) return;
	observedToken = next;
	resetCatalog();
});

/** Test-only owner reset: invalidates in-flight responses as well as visible state. */
export function _resetProjectsForTests(): void {
	resetCatalog();
}

/** Test-only preloaded catalog fixture with internally consistent load status. */
export function _setProjectsForTests(value: ProjectInfo[]): void {
	generation++;
	inFlight = null;
	loadedToken = get(token);
	projects.set(value);
	projectLoadStatus.set('loaded');
	projectLoadError.set(null);
}

function toProjectInfo(p: { id: string; name: string; status: string; accent_color?: string | null }): ProjectInfo {
	return { id: p.id, name: p.name, status: p.status, accent_color: p.accent_color ?? null };
}

export function enterProjectScope(p: ProjectPick): void {
	const known = get(projects).find((x) => x.id === p.id);
	const accent = p.accent_color ?? known?.accent_color ?? null;
	enterScope({ id: p.id, name: p.name, color: projectAccent(p.id, accent) });
}

/**
 * Make the active conversation's project authoritative for the UI scope.
 *
 * Selecting a conversation must never leave `uiScope` at the workspace while
 * the thread belongs to a project (cold-start restore, ⌘K, sidebar): the
 * accent color and every scope-derived API target (doc library, conductor,
 * new-chat creation) would operate against the wrong scope.
 *
 * - Thread row known with `project_id` → enter that project's scope.
 * - Thread row known with no project → exit to the workspace scope.
 * - Thread row unknown (e.g. hidden subagent thread not in the threads
 *   list) → leave the current scope untouched; we have no authority to
 *   change it and must not clobber the parent's project scope.
 *
 * The `projects` store is loaded on demand so the project's name/accent are
 * available even on cold start before the sidebar's initial fetch lands.
 */
export async function enterThreadScope(
	thread: Thread | null | undefined,
	canEnter: (() => boolean) | undefined = undefined,
): Promise<void> {
	if (!thread || (canEnter && !canEnter())) return;
	if (!thread.project_id) {
		if (!canEnter || canEnter()) exitScope();
		return;
	}
	let known = get(projects).find((x) => x.id === thread.project_id);
	if (!known) {
		await loadProjects();
		if (canEnter && !canEnter()) return;
		known = get(projects).find((x) => x.id === thread.project_id) ?? undefined;
	}
	if (canEnter && !canEnter()) return;
	enterProjectScope({
		id: thread.project_id,
		name: known?.name ?? 'Project',
		accent_color: known?.accent_color ?? null,
	});
}

export async function createProject(input: {
	name: string;
	description?: string;
	accent_color?: string | null;
}): Promise<ProjectInfo> {
	const currentToken = get(token);
	if (!currentToken) throw new Error('Not authenticated');
	const res = await fetch(`/api/projects?token=${encodeURIComponent(currentToken)}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			name: input.name,
			description: input.description ?? '',
			accent_color: input.accent_color ?? null,
		}),
	});
	if (!res.ok) throw new Error(await plainResponseError(res, 'Couldn’t create the project. Try again.'));
	const info = toProjectInfo((await res.json()).project);
	projects.update((list) => [...list, info]);
	return info;
}

export async function updateProject(
	id: string,
	patch: { name?: string; description?: string; accent_color?: string | null },
): Promise<void> {
	const currentToken = get(token);
	if (!currentToken) throw new Error('Not authenticated');
	const res = await fetch(`/api/projects/${id}?token=${encodeURIComponent(currentToken)}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch),
	});
	if (!res.ok) throw new Error(await plainResponseError(res, 'Could not update project. Try again.'));
	projects.update((list) =>
		list.map((p) =>
			p.id === id
				? {
						...p,
						...(patch.name !== undefined ? { name: patch.name } : {}),
						...(patch.accent_color !== undefined ? { accent_color: patch.accent_color } : {}),
					}
				: p,
		),
	);
	if (patch.name !== undefined) {
		const { loadThreads } = await import('./threadsLoad');
		// The rename is already saved; a failed refresh must not report it as failed.
		await loadThreads(currentToken).catch((error) => console.error('Could not refresh threads after project update', error));
	}
}

export async function archiveProject(id: string): Promise<void> {
	await _transition(id, 'archived');
}

export async function restoreProject(id: string): Promise<void> {
	await _transition(id, 'active');
}

async function _transition(id: string, to_status: 'archived' | 'active'): Promise<void> {
	const currentToken = get(token);
	if (!currentToken) throw new Error('Not authenticated');
	const res = await fetch(`/api/projects/${id}/transition?token=${encodeURIComponent(currentToken)}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ to_status }),
	});
	if (!res.ok) throw new Error(await plainResponseError(res, 'Could not change project status. Try again.'));
	projects.update((list) => list.map((p) => (p.id === id ? { ...p, status: to_status } : p)));
}
