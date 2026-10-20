/**
 * The Welcome copy, word for word from the approved onboarding story (section 2). The copy doc
 * `shared/onboarding/2026-10-19-setup-copy.md` quotes the same strings; change them together.
 */
export const WELCOME_TITLE = 'Welcome to CORSIA';
export const WELCOME_LINES = [
	'Your agents learn how you like things done and keep their own instructions and skills up to date, so you don’t have to.',
	'You can check that yourself. CORSIA measures how much of your time their work takes, and you can see it any time.',
] as const;
export const WELCOME_ROWS = [
	{ label: 'CORSIA handles:', text: 'the setup steps for connecting your tools, and checking work against what you asked before it reaches you.' },
	{ label: 'You decide:', text: 'what gets done. When a choice is yours to make, agents bring it to you with a recommendation.' },
	{ label: 'Asks you first:', text: 'sending messages or posting anything for you, making purchases, or opening files outside your work folder. You can let agents go ahead on things you approve often.' },
] as const;
export const WELCOME_ACCOUNT_LINE = 'Create your account. It stays on this computer.';
