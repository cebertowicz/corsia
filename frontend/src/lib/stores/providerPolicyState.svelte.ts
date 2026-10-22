import {
	loadProviderPolicy,
	saveProviderPolicy,
	saveUseWorkspace,
	type ProviderEntry,
	type ProviderPolicy,
} from '$lib/api/providerPolicy';
import { saveFailureText } from '$lib/saveFailure';

type Failure = { ok: false; conflict?: boolean; detail?: unknown; reached?: boolean };
/** A conflict says the setting moved; the controls are refreshed to show it before this line appears. */
const failureText = (result: Failure) => result.conflict
	? saveFailureText('It changed elsewhere and now shows the latest setting.', true)
	: saveFailureText(result.detail, result.reached !== false);

/** One policy owner for both workspace account controls and project overrides. */
export function createProviderPolicyState(projectId?: string | (() => string | undefined)) {
	const currentProjectId = () => typeof projectId === 'function' ? projectId() : projectId;
	let policy = $state<ProviderPolicy | null>(null);
	let error = $state('');
	let loading = $state(true);
	let pendingTargets = $state<Set<string>>(new Set());
	let failures = $state<Record<string, string>>({});
	const providerQueues = new Map<string, Promise<void>>();
	type PolicyPatch = Partial<ProviderEntry> | ((entry: ProviderEntry) => Partial<ProviderEntry>);

	async function load() {
		loading = true;
		error = '';
		failures = {};
		try {
			const result = await loadProviderPolicy(currentProjectId());
			if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
			else error = result.ok ? 'Could not load providers.' : result.error;
		} finally {
			loading = false;
		}
	}

	function setPending(target: string, pending: boolean) {
		const next = new Set(pendingTargets);
		if (pending) next.add(target);
		else next.delete(target);
		pendingTargets = next;
	}

	function setFailure(target: string, text: string | null) {
		const { [target]: _, ...rest } = failures;
		failures = text === null ? rest : { ...rest, [target]: text };
	}

	/** Re-read the policy without touching loading, error or failures, so the next save uses the current revision. */
	async function refresh() {
		const result = await loadProviderPolicy(currentProjectId());
		if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
	}

	function save(entry: ProviderEntry, patch: PolicyPatch, target = `provider:${entry.provider_key}`): Promise<void> {
		if (pendingTargets.has(target)) return Promise.resolve();
		setPending(target, true);
		setFailure(target, null);
		error = '';
		const previous = providerQueues.get(entry.provider_key) ?? Promise.resolve();
		let queued: Promise<void>;
		queued = previous.then(async () => {
			const current = policy?.providers.find((item) => item.provider_key === entry.provider_key) ?? entry;
			const resolvedPatch = typeof patch === 'function' ? patch(current) : patch;
			const result = await saveProviderPolicy({ ...current, ...resolvedPatch }, currentProjectId());
			if (result.ok && policy) {
				policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === entry.provider_key ? result.value : item) };
			} else if (!result.ok) {
				error = result.error;
				setFailure(target, failureText(result));
				if (result.conflict) await refresh();
			}
		}).finally(() => {
			setPending(target, false);
			if (providerQueues.get(entry.provider_key) === queued) providerQueues.delete(entry.provider_key);
		});
		providerQueues.set(entry.provider_key, queued);
		return queued;
	}

	async function toggleScope(useWorkspace: boolean) {
		const current = currentProjectId();
		const target = 'scope';
		if (!current || !policy || pendingTargets.has(target)) return;
		setPending(target, true);
		setFailure(target, null);
		error = '';
		try {
			const result = await saveUseWorkspace(current, useWorkspace, policy.override_revision ?? 0);
			if (result.ok) policy = result.value;
			else {
				error = result.error;
				setFailure(target, failureText(result));
				if (result.conflict) await refresh();
			}
		} finally {
			setPending(target, false);
		}
	}

	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) {
		void save(provider, (current) => ({ disabled: enabled ? current.disabled.filter((id) => id !== unitId) : [...current.disabled, unitId] }), `unit:${provider.provider_key}:${unitId}`);
	}

	function move(provider: ProviderEntry, unitId: string, direction: -1 | 1) {
		void save(provider, (current) => {
			const order = [...(current.order ?? current.units.map((unit) => unit.id))];
			const index = order.indexOf(unitId);
			const destination = index + direction;
			if (index < 0 || destination < 0 || destination >= order.length) return {};
			[order[index], order[destination]] = [order[destination], order[index]];
			return { order };
		}, `order:${provider.provider_key}:${unitId}`);
	}

	function isPending(target: string): boolean { return pendingTargets.has(target); }
	function hasFailure(target: string): boolean { return target in failures; }
	function failureTextFor(target: string): string | null { return failures[target] ?? null; }
	function hasControlFailure(): boolean { return Object.keys(failures).length > 0; }

	return {
		get policy() { return policy; },
		get error() { return error; },
		get loading() { return loading; },
		get busy() { return pendingTargets.size > 0; },
		isPending,
		hasFailure,
		failureText: failureTextFor,
		hasControlFailure,
		load,
		save,
		toggleScope,
		setEnabled,
		move,
	};
}
