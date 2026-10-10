import { writable, derived, type Readable } from 'svelte/store';
import bundleSelf from './build-info.json';

export interface BuildInfo {
	label: string;
	short_hash: string;
	full_hash: string;
	commit_iso: string;
	commit_subject: string;
	/** null = git could not say; never render it as clean. */
	dirty: boolean | null;
	build_iso: string;
}

export interface CurrentHead {
	short_hash: string;
	full_hash: string;
	commit_iso: string;
	commit_subject: string;
	/** null = git could not say; never render it as clean. */
	dirty: boolean | null;
}

export interface ServerVersion {
	frontend: BuildInfo;
	backend: BuildInfo & {
		start_iso: string;
		current_head: CurrentHead;
	};
}

export const myBuild: BuildInfo = bundleSelf as BuildInfo;

export const serverVersion = writable<ServerVersion | null>(null);

export const isFrontendStale: Readable<boolean> = derived(
	serverVersion,
	($s) => {
		if (!$s) return false;
		const a = myBuild.short_hash;
		const b = $s.frontend?.short_hash;
		if (!a || !b) return false;
		if (a === 'unknown' || b === 'unknown') return false;
		return a !== b;
	},
);

export const isBackendBehindOnDisk: Readable<boolean> = derived(
	serverVersion,
	($s) => {
		if (!$s) return false;
		const running = $s.backend?.short_hash;
		const onDisk = $s.backend?.current_head?.short_hash;
		if (!running || !onDisk) return false;
		if (running === 'unknown' || onDisk === 'unknown') return false;
		return running !== onDisk;
	},
);

export async function loadServerVersion(): Promise<void> {
	try {
		const res = await fetch('/api/version', { cache: 'no-store' });
		if (res.ok) serverVersion.set(await res.json());
	} catch (e) {
		console.warn('loadServerVersion failed:', e);
	}
}

export function reloadWithCacheBust(): void {
	const url = new URL(window.location.href);
	url.searchParams.set('cb', Date.now().toString());
	window.location.replace(url.toString());
}

export function formatHHMM(iso: string): string {
	if (!iso) return '--:--';
	const d = new Date(iso);
	if (isNaN(d.getTime())) return '--:--';
	const h = d.getHours().toString().padStart(2, '0');
	const m = d.getMinutes().toString().padStart(2, '0');
	return `${h}:${m}`;
}
