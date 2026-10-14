import { readSystemVersion, type SystemVersionSnapshot } from '$lib/stores/settingsSystemRestart';

export async function readVersionWithinFiveSeconds(): Promise<SystemVersionSnapshot | null> {
	const controller = new AbortController();
	let timer: ReturnType<typeof setTimeout> | null = null;
	try {
		const timeout = new Promise<null>((resolve) => {
			timer = setTimeout(() => { controller.abort(); resolve(null); }, 5_000);
		});
		return await Promise.race([readSystemVersion(controller.signal), timeout]);
	} finally {
		if (timer !== null) clearTimeout(timer);
	}
}

export function versionHashesAgree(version: SystemVersionSnapshot | null): boolean {
	const backend = version?.backend?.full_hash;
	const frontend = version?.frontend?.full_hash;
	const checkout = version?.backend?.current_head?.full_hash;
	return Boolean(
		backend && backend !== 'unknown'
		&& frontend && frontend !== 'unknown'
		&& frontend === backend
		&& (!checkout || (checkout !== 'unknown' && checkout === backend)),
	);
}
