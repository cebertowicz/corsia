/** Project roots live directly under the server-selected workspace projects folder. */

function trimmed(path: string): string {
	return path.replace(/\\/g, '/').replace(/\/+$/, '');
}

function parentDir(path: string): string {
	const clean = trimmed(path);
	return clean.slice(0, Math.max(clean.lastIndexOf('/'), 0));
}

/** True only for a direct child folder of `projectsRoot`. */
export function isProjectRootChoice(projectsRoot: string, path: string): boolean {
	const root = trimmed(projectsRoot);
	const clean = trimmed(path);
	return !!root && clean.length > root.length + 1 && parentDir(clean) === root;
}
