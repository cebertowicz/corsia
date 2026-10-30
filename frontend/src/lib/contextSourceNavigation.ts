export type ContextSourceOwnerTarget =
	| 'project-settings'
	| 'thread-settings'
	| 'providers'
	| 'workspace-settings'
	| 'system'
	| 'document';

type ContextSourceLike = {
	layer?: string;
	ownerSurface?: string;
	stableReference?: string;
	filePath?: string;
};

export function contextSourceFilePath(source: ContextSourceLike): string {
	const isPath = (value: string | undefined) => value && (/^\//.test(value) || /^[a-z]:[\\/]/i.test(value));
	return isPath(source.stableReference) ? source.stableReference! : isPath(source.filePath) ? source.filePath! : '';
}

function normalized(value: string | undefined): string {
	return (value ?? '').toLowerCase();
}

export function contextSourceOwnerTarget(source: ContextSourceLike): ContextSourceOwnerTarget | null {
	const layer = normalized(source.layer);
	const owner = normalized(source.ownerSurface);
	const reference = normalized(source.stableReference);

	if (['skills', 'portable-checkpoint', 'tool-registry', 'runtime-context', 'conductor-designation'].includes(owner) && !contextSourceFilePath(source)) return null;
	if (owner.includes('document') && contextSourceFilePath(source)) return 'document';
	if (layer === 'corsia') return contextSourceFilePath(source) ? 'document' : null;
	if (
		owner.includes('project') ||
		owner.includes('agent') ||
		reference.startsWith('project://') ||
		layer === 'project'
	) {
		return 'project-settings';
	}
	if (
		owner.includes('provider') ||
		owner.includes('model') ||
		reference.startsWith('model://') ||
		layer === 'model' ||
		layer === 'harness'
	) {
		return 'providers';
	}
	if (owner === 'workspace-settings' || layer === 'workspace') return 'workspace-settings';
	if (owner === 'context-builder' || owner.includes('system') || layer === 'system') return 'system';
	return contextSourceFilePath(source) ? 'document' : null;
}

export function contextSourceActionLabel(source: ContextSourceLike): string {
	const target = contextSourceOwnerTarget(source);
	if (target === 'project-settings') return 'Open Project settings';
	if (target === 'thread-settings') return 'Open chat settings';
	if (target === 'providers') return 'Open Providers';
	if (target === 'workspace-settings') return 'Open Context settings';
	if (target === 'system') return 'Open System';
	if (target === 'document') return 'Open Document';
	return '';
}

export function contextSourceOwnerLabel(source: ContextSourceLike): string {
	const target = contextSourceOwnerTarget(source);
	if (target === 'project-settings') return 'Project settings';
	if (target === 'thread-settings') return 'Chat settings';
	if (target === 'providers') return 'Providers';
	if (target === 'workspace-settings') return 'Context settings';
	if (target === 'system') return 'System context';
	if (target === 'document') return 'Document';
	return '';
}
