import { get, writable, type Readable } from 'svelte/store';

export const SETTINGS_SECTIONS = ['providers', 'connected-apps', 'skills', 'context', 'projects', 'keys', 'system', 'debug'] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export interface SettingsDrill {
	title: string;
	backLabel?: string;
	onBack: () => void;
}

/** Everything about where Settings is and how it was reached. */
export interface SettingsNavigation {
	section: SettingsSection;
	/** The project whose page is open under Projects; null shows the project list. */
	project: string | null;
	/** The project page a "What this project can use" row came from; Back returns to it. */
	fromProject: { id: string; name: string } | null;
	/** The app header while Settings is drilled into a page: it names the page and goes up one level. */
	drill: SettingsDrill | null;
	/** Counts deep-link opens (a gear, a chat link); a consumer acts once per new value. */
	openRequest: number;
	/**
	 * The thread Settings opened over: a file opened from Settings becomes its tab, and Back returns there
	 * instead of to Activity. Kept across a Settings → Documents → Settings round trip; null once the person
	 * leaves Settings by any other route.
	 */
	homeThread: string | null;
	/** An element in the opened section (its `data-settings-anchor`) to bring into view once, then cleared. */
	anchor: string | null;
}

/*
 * Settings navigation is one store, changed only by the functions below. Fields not listed are kept.
 *
 * requestSettingsSection      section, anchor, openRequest+1; project cleared unless section is projects; fromProject cleared
 * openWorkspaceSettings       requestSettingsSection('providers', anchor)
 * openProjectSettings         section=projects, project, anchor, openRequest+1; fromProject cleared
 * requestProviderCard         requestSettingsSection('providers') (and providerCardRequest)
 * openSectionFromProject      section, anchor, fromProject=project; project cleared (no openRequest bump: already inside Settings)
 * returnToProjectPage         section=projects, project=fromProject.id, fromProject cleared, anchor cleared
 * selectSettingsSection       section; project, fromProject and any pending anchor cleared
 * openSettingsProject         project (a row in the Projects list); pending anchor cleared
 * closeSettingsProject        project cleared; pending anchor cleared
 * setSettingsDrill            drill
 * recordSettingsHomeThread    homeThread: the active thread when Settings shows over one, kept while Documents shows, else null
 * closeSettingsBack           homeThread cleared (the selection that follows would clear it too)
 * clearSettingsAnchor         anchor cleared (an anchored element calls it once it has scrolled)
 * resetSettingsNavigation     every field to its initial value
 */
const initial = (): SettingsNavigation => ({
	section: 'providers', project: null, fromProject: null, drill: null,
	openRequest: 0, homeThread: null, anchor: null,
});
const state = writable<SettingsNavigation>(initial());
export const settingsNavigation: Readable<SettingsNavigation> = { subscribe: state.subscribe };

function change(patch: (current: SettingsNavigation) => Partial<SettingsNavigation>) {
	state.update((current) => ({ ...current, ...patch(current) }));
}

/** The harness connection or cloud provider whose Providers card should open expanded and scroll into view. */
export const providerCardRequest = writable<string | null>(null);

/** Links already in saved messages name a section that has since moved. */
const MOVED_SECTIONS: Record<string, { section: SettingsSection; anchor: string }> = {
	'system#connected-apps': { section: 'connected-apps', anchor: 'connected-apps' },
};

export function requestSettingsSection(section: SettingsSection, anchor: string | null = null) {
	const moved = MOVED_SECTIONS[`${section}#${anchor}`];
	if (moved) ({ section, anchor } = moved);
	change((s) => ({
		section, anchor, fromProject: null,
		project: section === 'projects' ? s.project : null,
		openRequest: s.openRequest + 1,
	}));
}

/** The workspace gear: Settings at its first section. */
export function openWorkspaceSettings(anchor: string | null = null) {
	requestSettingsSection('providers', anchor);
}

/** A project's gear: Settings at Projects, on that project's page. */
export function openProjectSettings(projectId: string, anchor: string | null = null) {
	change((s) => ({
		section: 'projects', project: projectId, fromProject: null, anchor, openRequest: s.openRequest + 1,
	}));
}

/** Inside Settings: a project page's "What this project can use" row opens that section at the project's level. */
export function openSectionFromProject(section: SettingsSection, project: { id: string; name: string }, anchor: string | null) {
	change(() => ({ section, project: null, fromProject: project, anchor }));
}

/** Back from a section opened by a project page: that project's page again. */
export function returnToProjectPage() {
	change((s) => ({ section: 'projects', project: s.fromProject?.id ?? null, fromProject: null, anchor: null }));
}

/** A tap on the section list. */
export function selectSettingsSection(section: SettingsSection) {
	change(() => ({ section, project: null, fromProject: null, anchor: null }));
}

/** A row in the Projects list opens that project's page. */
export function openSettingsProject(projectId: string) {
	change(() => ({ project: projectId, anchor: null }));
}

/** Back from a project's page to the project list. */
export function closeSettingsProject() {
	change(() => ({ project: null, anchor: null }));
}

export function setSettingsDrill(drill: SettingsDrill | null) {
	change(() => ({ drill }));
}

/**
 * Settings keeps the thread it opened over, so a file opened there has a tab to land in. Called with every
 * view the app shows: Documents is the one view Settings sends the person to and back from (Add existing,
 * Browse), so it keeps the thread; any other view is the person leaving Settings.
 */
export function recordSettingsHomeThread(view: string, activeThread: string | null) {
	change((s) => {
		if (view === 'settings') return { homeThread: activeThread ?? s.homeThread };
		if (view === 'documents') return {};
		return { homeThread: null };
	});
}

export function clearSettingsAnchor(anchor?: string) {
	change((s) => (anchor === undefined || s.anchor === anchor ? { anchor: null } : {}));
}

// The last open request a SettingsView acted on. It outlives a mount because a gear's
// request usually arrives before Settings mounts.
let consumedOpenRequest = 0;

export function resetSettingsNavigation() {
	consumedOpenRequest = 0;
	state.set(initial());
}

/** True once per new open request. */
export function consumeSettingsOpenRequest(): boolean {
	const { openRequest } = get(state);
	if (openRequest === consumedOpenRequest) return false;
	consumedOpenRequest = openRequest;
	return true;
}

/**
 * Put on every `data-settings-anchor` element. When this element mounts, or a request for it
 * arrives while it is mounted, it scrolls into view and clears the request: the element that
 * renders the anchor decides when it can be reached, however late its data arrives.
 */
export function settingsAnchor(node: HTMLElement, id: string | null | undefined) {
	let mine = id;
	const consume = (s: SettingsNavigation) => {
		if (!mine || s.anchor !== mine) return;
		node.scrollIntoView({ block: 'start' });
		clearSettingsAnchor(mine);
	};
	const stop = state.subscribe(consume);
	return {
		update(next: string | null | undefined) {
			mine = next;
			consume(get(state));
		},
		destroy: stop,
	};
}

/** Open one registered server's Providers card; a server id never collides with a provider name. */
export function requestServerCard(serverId: string) {
	requestProviderCard(serverId);
}

export function requestProviderCard(connection: string) {
	providerCardRequest.set(connection);
	requestSettingsSection('providers');
}

/** Close Settings: back to the thread a deep link came from, else to the fallback (Activity). */
export async function closeSettingsBack(selectThread: (id: string) => Promise<unknown>, fallback: () => void) {
	const id = get(state).homeThread;
	change(() => ({ homeThread: null }));
	if (id) await selectThread(id);
	else fallback();
}
