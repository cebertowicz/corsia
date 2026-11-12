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

/**
 * Wait until the side panels have finished sliding open or shut on a desktop. The canvas keeps narrowing or widening,
 * and its text rewrapping, until their width transition ends, so a line placed or measured before then keeps moving
 * (and a click on it finds it unstable and scrolls it into view). Polled for the same reason as above.
 */
export async function waitForPanelsToSettle(page: Page): Promise<void> {
	await page.waitForFunction(() => !document.getAnimations()
		.some((animation) => animation instanceof CSSTransition
			&& animation.transitionProperty === 'width'
			&& animation.playState === 'running'));
}
