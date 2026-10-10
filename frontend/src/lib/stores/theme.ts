import { writable, get } from 'svelte/store';

export type ThemeSetting = 'dark' | 'light' | 'auto';
type ResolvedTheme = 'dark' | 'light';

const stored = typeof localStorage !== 'undefined' ? localStorage.getItem('corsia_theme') as ThemeSetting : null;

export const themeSetting = writable<ThemeSetting>(stored ?? 'auto');

export const theme = writable<ResolvedTheme>('dark');

function getSystemTheme(): ResolvedTheme {
	if (typeof window !== 'undefined' && window.matchMedia) {
		return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
	}
	return 'dark';
}

function applyTheme(resolved: ResolvedTheme) {
	theme.set(resolved);
	if (typeof document !== 'undefined') {
		document.documentElement.setAttribute('data-theme', resolved);
	}
}

function resolveAndApply(setting: ThemeSetting) {
	const resolved = setting === 'auto' ? getSystemTheme() : setting;
	applyTheme(resolved);
}

themeSetting.subscribe(setting => {
	resolveAndApply(setting);
	if (typeof localStorage !== 'undefined') {
		localStorage.setItem('corsia_theme', setting);
	}
});

if (typeof window !== 'undefined' && window.matchMedia) {
	window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
		if (get(themeSetting) === 'auto') {
			resolveAndApply('auto');
		}
	});
}
