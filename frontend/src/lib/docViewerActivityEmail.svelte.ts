import { getContext, setContext } from 'svelte';
import { fromStore } from 'svelte/store';
import { EMAIL_DRAFT_SETTLED } from '$lib/emailDraft';
import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';
import { reload as reloadAttentionQueue } from '$lib/stores/attentionQueueState.svelte';
import { ACTIVITY_EMAIL_REVIEW, type ActivityEmailReview } from '$lib/activityEmailReview';
import { createReviewBarState } from '$lib/reviewBarState.svelte';

/** Connect the Activity email controls to this viewer's review bar. */
export function createViewerActivityEmail(
	artifactId: () => string | null,
	review: () => { requestChanges: () => unknown } | undefined,
) {
	const bar = createReviewBarState();
	const activityDraft = !!getContext(EMAIL_DRAFT_SETTLED);
	const headers = fromStore(emailDraftHeaders);
	const active = () => { const id = artifactId(); return activityDraft && !!id && !!headers.current[id]; };
	let focusEmailRequest: (() => void) | undefined;
	setContext<ActivityEmailReview>(ACTIVITY_EMAIL_REVIEW, {
		get enabled() { return active() && bar.visible && bar.phase === 'ask' && !bar.read?.person_step; },
		get noting() { return active() && (bar.phase === 'noting' || (bar.phase === 'sending' && bar.sendingFrom === 'noting')); },
		requestChanges: () => { void review()?.requestChanges(); },
		registerRequest: (focus) => { focusEmailRequest = focus; return () => { if (focusEmailRequest === focus) focusEmailRequest = undefined; }; },
		focusRequest: () => focusEmailRequest?.(),
	});
	return { bar, get active() { return active(); }, reloadAttention: () => { void reloadAttentionQueue(); } };
}
