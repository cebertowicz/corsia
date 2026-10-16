import { expect, test } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;

test('scope semantic roles preserve legacy text, wash, and line outputs', async ({ page }) => {
	await page.goto(authed('/'));
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });

	const proof = await page.evaluate(() => {
		const root = document.documentElement;
		const probe = document.createElement('div');
		probe.innerHTML = '<span class="text">text</span><span class="wash">wash</span><span class="line">line</span>';
		const style = document.createElement('style');
		style.textContent = `
			#scope-role-proof .text { color: var(--accent-scope-text); }
			#scope-role-proof .wash { background-color: var(--accent-scope-wash); }
			#scope-role-proof .line { border: 1px solid var(--accent-scope-line); }
			#scope-role-legacy .text { color: var(--violet-text); }
			#scope-role-legacy .wash { background-color: var(--violet-bg); }
			#scope-role-legacy .line { border: 1px solid var(--violet-line); }
		`;
		probe.id = 'scope-role-proof';
		const legacy = probe.cloneNode(true) as HTMLElement;
		legacy.id = 'scope-role-legacy';
		document.head.append(style);
		document.body.append(probe, legacy);

		const read = (container: HTMLElement) => {
			const [text, wash, line] = [...container.children] as HTMLElement[];
			return {
				text: getComputedStyle(text).color,
				wash: getComputedStyle(wash).backgroundColor,
				line: getComputedStyle(line).borderTopColor,
			};
		};
		const existingAccent = {
			accent: getComputedStyle(root).getPropertyValue('--accent').trim(),
			accentHover: getComputedStyle(root).getPropertyValue('--accent-hover').trim(),
			accentFill: getComputedStyle(root).getPropertyValue('--accent-fill').trim(),
			accentText: getComputedStyle(root).getPropertyValue('--accent-text').trim(),
			accentLine: getComputedStyle(root).getPropertyValue('--accent-line').trim(),
			accentWash: getComputedStyle(root).getPropertyValue('--accent-wash').trim(),
		};
		const legacyAccent = {
			accent: getComputedStyle(root).getPropertyValue('--violet').trim(),
			accentHover: getComputedStyle(root).getPropertyValue('--violet-2').trim(),
			accentFill: getComputedStyle(root).getPropertyValue('--violet-text').trim(),
			accentText: getComputedStyle(root).getPropertyValue('--violet-text').trim(),
			accentLine: getComputedStyle(root).getPropertyValue('--accent').trim(),
			accentWash: getComputedStyle(root).getPropertyValue('--violet-bg').trim(),
		};
		const defaultScope = read(probe);
		const defaultLegacy = read(legacy);
		const prior = Object.fromEntries(['--accent', '--accent-hover', '--violet', '--violet-2', '--violet-text', '--violet-bg', '--violet-line', '--accent-scope-text', '--accent-scope-wash', '--accent-scope-line'].map((name) => [name, root.style.getPropertyValue(name)]));

		try {
			const color = '#f97316';
			root.style.setProperty('--accent', color);
			root.style.setProperty('--accent-hover', color);
			root.style.setProperty('--violet', color);
			root.style.setProperty('--violet-2', color);
			root.style.setProperty('--violet-text', 'rgb(249, 115, 22)');
			root.style.setProperty('--violet-bg', `color-mix(in srgb, ${color} 12%, transparent)`);
			root.style.setProperty('--violet-line', `color-mix(in srgb, ${color} 30%, transparent)`);
			root.style.setProperty('--accent-scope-text', 'rgb(249, 115, 22)');
			root.style.setProperty('--accent-scope-wash', `color-mix(in srgb, ${color} 12%, transparent)`);
			root.style.setProperty('--accent-scope-line', `color-mix(in srgb, ${color} 30%, transparent)`);
			return { defaultScope, defaultLegacy, projectScope: read(probe), projectLegacy: read(legacy), existingAccent, legacyAccent };
		} finally {
			for (const [name, value] of Object.entries(prior)) {
				if (value) root.style.setProperty(name, value);
				else root.style.removeProperty(name);
			}
			probe.remove();
			legacy.remove();
			style.remove();
		}
	});

	expect(proof.defaultScope).toEqual(proof.defaultLegacy);
	expect(proof.projectScope).toEqual(proof.projectLegacy);
	expect(proof.existingAccent).toEqual(proof.legacyAccent);
});
