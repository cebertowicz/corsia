import type { ArtifactTab, RevisionCheck } from './artifactTabTypes';

/** The title a document tab opened by path gets: its file name. */
export function defaultTabTitle(path: string): string {
	return path.split('/').filter(Boolean).at(-1) ?? path;
}

/**
 * A relocated document tab's title. A tab still showing its file name follows the new one, as
 * the chat card does; a title its opener chose survives the move.
 */
export function relocatedTitle(prev: ArtifactTab, check: RevisionCheck): string {
	if (!check.path || check.path === prev.path) return prev.title;
	return prev.title === defaultTabTitle(prev.path) ? defaultTabTitle(check.path) : prev.title;
}
