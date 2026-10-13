# Getting the person a web search key

Use this for first-run task `Connect web search` and any time later the person asks to turn on web search. Brave's pages were checked read-only in the agent browser on 2026-10-09. Follow it exactly; never improvise a step, a link or a price.

**Rules.** You never see, ask for, read or repeat the key or a password. The person types their password themselves and pastes the key into Settings › Keys; never accept either in chat. If they paste a key in chat anyway, post `Please don't paste keys here. Delete that message, then put the key in Settings › Keys.` and carry on. Browser steps follow `references/browser-person-steps.md`. There is no fallback search: without a key, web search stays off.

## 1. Explain, then ask

One `post_update`, word for word:

> Web search lets me look things up on the web for you. It uses Brave Search. Brave's free plan still asks for a credit card; search costs $5 per 1,000 searches and includes $5 of free credit every month, so about 1,000 searches a month cost nothing. Do you already have a Brave Search API account (the developer one, separate from the Brave browser)?

End your turn. Not now: `OK. Settings › Keys has it whenever you want it.` Close their task and task `Connect web search` with `You chose not now. Settings › Keys has it.`

## 2. Open the right page

Open it with `browser_open` and `show_window: true`, tell the person in one `post_update` that a window opened, and fill only the email they chose at install (the saved email; if none is saved, ask for one in chat first).

| They have an account | Open | Fill |
|---|---|---|
| Yes | `https://api-dashboard.search.brave.com/login` | Email |
| No | [Brave Search API sign-up](https://api-dashboard.search.brave.com/register) | Email |

Sign-up asks only for an email and a password; **Create account** stays disabled until the password is filled, and Brave emails a link to confirm the account. Tell them: type your password yourself, press **Create account**, then click the link in the email Brave sends you. These are sign-in pages, so watch per `browser-person-steps.md` B, reading only the URL and title; a captcha with no password field is watched per A.

## 3. Take them to the key

Open [API Keys](https://api-dashboard.search.brave.com/app/keys). A page that sends them back to the login page is theirs (B). If they have no plan yet, they open [Plans](https://api-dashboard.search.brave.com/app/subscriptions/subscribe), choose **Search** and add their card; that is theirs too. When the API Keys page is showing, post once: `Press Add API Key, name it CORSIA and copy the key. Then open [Settings › Keys](/settings/keys), paste it under Web search, enter your CORSIA password and press Save. Check tells you right away whether it works. The key stays on this Mac; agents use it but can never see it. Tell me when it's in.` Read the URL and title only; never read the key off the page. Add their task `Add your web search key`; task `Connect web search` depends on it. End your turn.

## 4. Help with a step

If the person says a step doesn't match what they see, answer only from this list, then ask them to tell you when the key is in:

- No confirmation email: `Check your spam folder. If it isn't there, sign up again with the same email.`
- Plans page asks them to log in: `Log in with the email and password you just created, then open Plans again.`
- Unsure which plan: `Choose Search. It's the one with web results; the other plans aren't needed.`
- Worried about cost: `Search costs $5 per 1,000 searches, and the $5 of free credit each month covers about 1,000. You only pay past that.`
- Can't find where to paste it: `Settings › Keys, under Web search: [Settings › Keys](/settings/keys).`

Anything else: `Tell me what's on your screen and I'll help.`

## 5. After they say it's in

| Then | Say | Record |
|---|---|---|
| The person says it's in, or any later wake | Run one `web_search` for `CORSIA`. Results, or none: `Web search works.` | `mark_done` their task with evidence in plain words (what they said, what you checked); task `Connect web search` done |
| `Web search: key refused.` | `Brave didn't accept that key. Copy it again from API Keys and save it in Settings › Keys; Check tells you right away.` | Replace their task with `Add a key Brave accepts`; close the old one (`Key refused`) |
| `Web search failed: [reason class]` | `Web search didn't work just now: [reason, never the key]. Your key is saved; I'll try again next time I'm working.` Once per reason | Note `I'll try again (told you: [reason class])`; later wakes re-check silently |
| No key found | `I don't see it in Settings › Keys yet. Paste it under Web search and press Save.` | Their task stays open |

A tool result starting `Error:` is a failed search; report it without guessing a reason. You may add a key entry and read entries' metadata, nothing more.
