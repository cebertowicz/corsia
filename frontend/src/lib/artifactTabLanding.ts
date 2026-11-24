import type { ArtifactTab, OpenArtifactInput, ThreadArtifactWorkspace } from './artifactTabTypes';

/** The landing an open asks for, as tab fields: none, the first unread, or the one thread it names. */
export function landingOf(input: OpenArtifactInput): Pick<ArtifactTab, 'landOn' | 'landOnCommentId'> {
	if (!input.landOn) return {};
	return input.landOnCommentId ? { landOn: input.landOn, landOnCommentId: input.landOnCommentId } : { landOn: input.landOn };
}

/** A new landing replaces the tab's earlier one whole: a thread named by an earlier open is not carried over. */
export function withLanding(tab: ArtifactTab, input: OpenArtifactInput): ArtifactTab {
	const { landOnCommentId: _earlier, ...rest } = tab;
	return { ...rest, ...landingOf(input) };
}

/** Clears a tab's landing once the viewer has acted on it, so it never fires twice. */
export function consumeLanding(ws: ThreadArtifactWorkspace, instanceId: string): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0 || !ws.tabs[idx].landOn) return null;
	const tabs = ws.tabs.slice();
	const { landOn: _consumed, landOnCommentId: _thread, ...rest } = tabs[idx];
	tabs[idx] = rest;
	return { ...ws, tabs };
}
