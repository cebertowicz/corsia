import type { ThreadArtifactWorkspace } from './artifactTabTypes';

/** Clears a tab's landing once the viewer has acted on it, so it never fires twice. */
export function consumeLanding(ws: ThreadArtifactWorkspace, instanceId: string): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0 || !ws.tabs[idx].landOn) return null;
	const tabs = ws.tabs.slice();
	const { landOn: _consumed, ...rest } = tabs[idx];
	tabs[idx] = rest;
	return { ...ws, tabs };
}
