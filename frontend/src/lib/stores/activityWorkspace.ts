/**
 * Activity's own pane holds the one item open there. It is keyed apart from every chat, so opening items in
 * Activity never adds tabs to the chats they came from, and it is never saved: Activity reopens on its top item.
 */
const ACTIVITY_WORKSPACE_PREFIX = 'activity:';

export function activityWorkspaceKey(projectId: string | null): string {
	return `${ACTIVITY_WORKSPACE_PREFIX}${projectId ?? 'workspace'}`;
}

export function isActivityWorkspace(key: string): boolean {
	return key.startsWith(ACTIVITY_WORKSPACE_PREFIX);
}
