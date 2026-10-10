/**
 * Whether a file is an image is the backend's answer alone: the artifact `type`,
 * the revision probe's `media_type`, or a text open's typed `image_file` refusal.
 * The canvas never infers it from a path.
 */

/** URL of the file's bytes on the authenticated file route; `revision` busts the cache after a change. */
export function imageContentUrl(opts: {
	path: string;
	projectId?: string | null;
	token: string;
	revision?: string | null;
}): string {
	const query = new URLSearchParams({ token: opts.token, path: opts.path, raw: 'true' });
	if (opts.projectId) query.set('project_id', opts.projectId);
	if (opts.revision) query.set('v', opts.revision);
	return `/api/files/content?${query.toString()}`;
}
