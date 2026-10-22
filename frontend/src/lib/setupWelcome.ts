/**
 * The Welcome copy, word for word from the approved onboarding story (section 2). The copy doc
 * `shared/onboarding/2026-10-19-setup-copy.md` quotes the same strings; change them together.
 */
export const WELCOME_TITLE = 'Welcome to CORSIA';
export const WELCOME_LINES = [
	'Your agents learn how you like things done and keep their own instructions and skills up to date, so you don’t have to.',
	'You can check that yourself: CORSIA measures the time you spend on work here, including keeping an eye on your agents.',
] as const;
export const WELCOME_ROWS = [
	{ label: 'CORSIA handles:', text: 'the setup steps for connecting your tools, and checking work against what you asked before it reaches you.' },
	{ label: 'You decide:', text: 'what gets done. When a choice is yours to make, agents bring it to you with a recommendation.' },
	{ label: 'Asks you first:', text: 'sending or posting as you, spending money, and opening files outside your work folder.' },
] as const;
export const ACCOUNT_LEDE = 'Your account and password stay on this computer.';
export const EMAIL_HINT = 'Used only to sign you up for services you choose, such as web search.';
