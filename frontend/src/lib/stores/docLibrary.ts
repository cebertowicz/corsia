import { derived, get, writable } from 'svelte/store';
import { uiScope } from '$lib/stores/uiScope';
import {
	fetchTree, patchDescription, registerArtifact, type TreeRoot
} from '$lib/api/docs';
import { relLabel } from '$lib/time';
import type { FreshnessState } from '$lib/docFreshness';

export type DescState = 'ai' | 'edited' | 'pending' | 'none';

export interface DocEntry {
	id: string;
	artifactId: string | null;
	sourceId: string;
	path: string;
	abs: string;
	name: string;
	description: string;
	descState: DescState;
	skill?: boolean;
	tmp?: boolean;
	tokens: number;
	updatedAt: string;
	updatedTs: number;
	updatedLabel: string;
	searchKey: string;
	ownerThread?: string | null;
	ownerThreadId?: string | null;
	ownerName?: string | null;
	freshnessState?: FreshnessState | null;
}

export interface DocSource {
	id: string;
	label: string;
	kind: 'workspace' | 'repo';
	path: string;
	capped?: boolean;
}

const DIR_DESCRIPTIONS: Record<string, string> = {
	working: 'Active drafts + investigation notes — unindexed until promoted',
	skills: 'Behaviors the agent can load — markdown, editable here',
	'test-scenarios': 'Canonical YAML scenario library'
};
export function dirDescription(dir: string): string {
	return DIR_DESCRIPTIONS[dir] ?? '';
}

export function secondaryDocumentText(entry: Pick<DocEntry, 'descState' | 'description' | 'ownerName'>): string {
	if (entry.descState === 'pending') return `description pending — queued for sweep${entry.ownerName ? ` · Owner: ${entry.ownerName}` : ''}`;
	return [entry.description, entry.ownerName ? `Owner: ${entry.ownerName}` : ''].filter(Boolean).join(' · ');
}

export const docSources = writable<DocSource[]>([]);
export const docLibrary = writable<DocEntry[]>([]);
export const libraryLoading = writable(false);
export const libraryError = writable<string | null>(null);

function mapRoots(roots: TreeRoot[]): { sources: DocSource[]; entries: DocEntry[] } {
	const sources: DocSource[] = [];
	const entries: DocEntry[] = [];
	for (const r of roots) {
		sources.push({ id: r.id, label: r.label, kind: r.kind, path: r.path, capped: r.capped });
		for (const n of r.nodes) {
			const abs = `${r.path}/${n.rel}`;
			const a = n.artifact;
			const updatedAt = a?.updated_at ?? new Date(n.mtime * 1000).toISOString();
			const description = a?.description ?? '';
			entries.push({
				id: abs,
				artifactId: a?.id ?? null,
				sourceId: r.id,
				path: n.rel,
				abs,
				name: n.name,
				description,
				descState: a ? (a.description ? 'ai' : 'pending') : 'none',
				skill: n.rel.startsWith('skills/'),
				tmp: n.tmp,
				tokens: Math.max(1, Math.round(n.size / 4)),
				updatedAt,
				updatedTs: Date.parse(updatedAt) || n.mtime * 1000,
				updatedLabel: relLabel(updatedAt),
				searchKey: `${n.rel} ${description} ${n.owner_name ?? ''}`.toLowerCase(),
				ownerThread: a?.owner_thread ?? null,
				ownerThreadId: a?.owner_thread_id ?? null,
				ownerName: n.owner_name ?? null,
				freshnessState: n.freshness?.state ?? null
			});
		}
	}
	return { sources, entries };
}

export async function loadLibrary() {
	libraryLoading.set(true);
	libraryError.set(null);
	try {
		const roots = await fetchTree(get(uiScope)?.id ?? null);
		const { sources, entries } = mapRoots(roots);
		docSources.set(sources);
		docLibrary.set(entries);
	} catch (e) {
		libraryError.set(e instanceof Error ? e.message : String(e));
	} finally {
		libraryLoading.set(false);
	}
}

export const skillDocs = derived(docLibrary, ($d) => $d.filter((x) => x.skill));
export const pendingCount = derived(docLibrary, ($d) => $d.filter((x) => x.descState === 'pending').length);

export const docActivity = derived(docLibrary, ($d) =>
	$d
		.filter((x) => x.artifactId)
		.sort((a, b) => b.updatedTs - a.updatedTs)
		.slice(0, 20)
		.map((x) => ({
			id: x.id,
			kind: (x.descState === 'pending' ? 'added' : 'described') as 'added' | 'described',
			text: x.descState === 'pending' ? `${x.path} registered — description queued` : `${x.path} — ${x.description}`,
			at: x.updatedLabel,
			actor: x.ownerThread ?? 'unknown thread'
		}))
);

async function ensureArtifact(e: DocEntry): Promise<string> {
	if (e.artifactId) return e.artifactId;
	const row = await registerArtifact(e.abs, { projectId: get(uiScope)?.id ?? null });
	docLibrary.update((l) =>
		l.map((d) => (d.abs === e.abs ? { ...d, artifactId: row.id, descState: d.description ? d.descState : 'pending' } : d))
	);
	return row.id;
}

export async function updateDescription(entry: DocEntry, text: string) {
	const aid = await ensureArtifact(entry);
	await patchDescription(aid, text);
	docLibrary.update((l) =>
		l.map((d) => (d.abs === entry.abs ? { ...d, description: text, descState: 'edited' } : d))
	);
}
