import posthog from 'posthog-js';

let initialized = false;
let initFailed = false;

interface ProductEventEnv {
	PUBLIC_POSTHOG_API_KEY?: string;
	PUBLIC_POSTHOG_HOST?: string;
	PUBLIC_POSTHOG_PROJECT?: string;
}

function readEnv(): ProductEventEnv {
	try {
		return (import.meta.env ?? {}) as ProductEventEnv;
	} catch {
		return {};
	}
}

function init(): void {
	if (initialized || initFailed) return;
	const env = readEnv();
	const apiKey = env.PUBLIC_POSTHOG_API_KEY;
	const host = env.PUBLIC_POSTHOG_HOST;
	if (!apiKey || !host) {
		initFailed = true;
		return;
	}
	try {
		posthog.init(apiKey, {
			api_host: host,
			capture_pageview: false,
			autocapture: false,
			persistence: 'localStorage+cookie',
		});
		initialized = true;
	} catch (err) {
		initFailed = true;
		try {
			console.debug('posthog init failed (silent):', err);
		} catch {
		}
	}
}

export function emitProductEvent(
	category: string,
	action: string,
	properties: Record<string, unknown> = {},
): void {
	try {
		init();
		if (!initialized) return;
		const env = readEnv();
		posthog.capture(`${category}.${action}`, {
			project: env.PUBLIC_POSTHOG_PROJECT ?? 'corsia',
			...properties,
		});
	} catch (err) {
		try {
			console.debug('posthog emit failed (silent):', err);
		} catch {
		}
	}
}

export function identifyUser(
	userId: string,
	traits: Record<string, unknown> = {},
): void {
	try {
		init();
		if (!initialized) return;
		posthog.identify(userId, traits);
	} catch (err) {
		try {
			console.debug('posthog identify failed (silent):', err);
		} catch {
		}
	}
}

export function _resetForTests(): void {
	initialized = false;
	initFailed = false;
}
