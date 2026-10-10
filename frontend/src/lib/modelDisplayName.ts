/**
 * The user-facing name of an external harness model, shared with
 * `external_model_display_name` in backend/subagents/model_display_name.py; both run
 * testdata/model-display-names.json. The input is a model id, never a stored
 * display name.
 */
function claudeDisplayName(words: string[]): string {
	const kept = words.length > 1 && /^\d{8}$/.test(words[words.length - 1]) ? words.slice(0, -1) : words;
	const name = kept.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
	return name.replace(/\b(\d) (\d)\b/g, '$1.$2');
}

export function externalModelDisplayName(model: string): string {
	const slash = model.indexOf('/');
	const name = slash === -1 ? model : model.slice(slash + 1);
	if (name.startsWith('claude')) return claudeDisplayName(name.split('-'));
	if (name === 'gpt-5-codex') return 'GPT-5 Codex';
	if (name.startsWith('gpt-')) return 'GPT-' + name.slice(4).replace(/-/g, ' ');
	return name.replace(/-/g, ' ').replace(/\b[a-z]/g, (char) => char.toUpperCase());
}
