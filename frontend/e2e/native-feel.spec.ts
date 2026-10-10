import { expect, test } from '@playwright/test';
import { loginByUrl } from './support/corsia';

test.describe('@surface:chat @surface:navigation native-feel contracts', () => {
	test('type ramp resolves platform-correct sizes', async ({ page }) => {
		await loginByUrl(page);
		const isMobile = (page.viewportSize()?.width ?? 1280) <= 768;
		const ramp = await page.evaluate(() => {
			const s = getComputedStyle(document.documentElement);
			return {
				body: s.getPropertyValue('--text-body').trim(),
				subhead: s.getPropertyValue('--text-subhead').trim(),
				input: s.getPropertyValue('--text-input').trim(),
				pressScale: s.getPropertyValue('--press-scale').trim(),
			};
		});
		expect(ramp.body).toBe(isMobile ? '17px' : '15px');
		expect(ramp.subhead).toBe(isMobile ? '15px' : '13px');
		expect(ramp.input).toBe('16px');
		expect(ramp.pressScale).toBe('0.96');
	});

	test('composer declares the native keyboard contract', async ({ page }) => {
		await loginByUrl(page);
		const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
		await expect(composer).toBeVisible();
		await expect(composer).toHaveAttribute('enterkeyhint', 'enter');
		await expect(composer).toHaveAttribute('autocapitalize', 'sentences');
		await expect(composer).toHaveAttribute('autocorrect', 'on');
	});

	test('queue remains a Send hold affordance instead of a separate control', async ({ page }) => {
		await loginByUrl(page);
		const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
		await composer.fill('Hold this message');

		await expect(page.locator('.queue-btn')).toHaveCount(0);
		const send = page.locator('.send-btn');
		await expect(send).toHaveAttribute('aria-label', 'Send message');
		// Nothing on this control may become a pan. A pan is a `pointercancel`,
		// and a `pointercancel` is what took the hold-to-queue gesture away on
		// iOS before it ever armed (USER-REPORTS #1).
		await expect(send).toHaveCSS('touch-action', 'none');

		await send.dispatchEvent('pointerdown', { clientX: 0, clientY: 0 });
		// The hold is visible while it is being made, not only once released.
		await expect(send).toHaveClass(/queue-holding/);
		await expect(send).toHaveAttribute(
			'aria-label',
			'Release to queue after the current turn',
			{ timeout: 2_000 },
		);
		await expect(send).toHaveClass(/queue-armed/);

		// Travel is a drag, not a hold — the one disarm that survives.
		await send.dispatchEvent('pointermove', { clientX: 0, clientY: 400 });
		await expect(send).not.toHaveClass(/queue-armed/);
		await expect(send).toHaveAttribute('aria-label', 'Send message');
	});

	test('app shell never rubber-bands as a page', async ({ page }) => {
		await loginByUrl(page);
		const overscroll = await page.evaluate(() => ({
			html: getComputedStyle(document.documentElement).overscrollBehavior,
			body: getComputedStyle(document.body).overscrollBehavior,
		}));
		expect(overscroll.html).toBe('none');
		expect(overscroll.body).toBe('none');
	});

	test('reduced-motion collapses transitions app-wide', async ({ page }) => {
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await loginByUrl(page);
		const durationMs = await page.evaluate(() => {
			const el = document.querySelector('.rail-link') ?? document.body;
			const raw = getComputedStyle(el).transitionDuration.split(',')[0].trim();
			return raw.endsWith('ms') ? parseFloat(raw) : parseFloat(raw) * 1000;
		});
		expect(durationMs).toBeLessThan(1);
	});
});
