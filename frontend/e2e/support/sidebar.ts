import type { Page } from '@playwright/test';

/**
 * Wait until the main surface has finished sliding across the phone rail.
 *
 * Polls for a running transform transition instead of awaiting
 * `animation.finished`: that promise rejects with an AbortError ("The user
 * aborted a request.") whenever the transition is cancelled or replaced, for
 * example when a re-render restyles `main.main` mid-slide. A poll tolerates both.
 */
export async function waitForMainSlideToSettle(page: Page): Promise<void> {
	await page.waitForFunction(() => !(document.querySelector('main.main')?.getAnimations() ?? [])
		.some((animation) => animation instanceof CSSTransition
			&& animation.transitionProperty === 'transform'
			&& animation.playState === 'running'));
}
