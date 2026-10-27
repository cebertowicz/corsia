# Getting the person a web search key

Use this for first-run task `Connect web search` and any time later the person asks to turn on web search. On the app path, use the server-seeded `Web search` task instead of creating `Connect web search`; keep the person-step tasks and dependencies this file needs, and mark the seeded task done or skipped. Brave's pages were checked read-only in the agent browser on 2026-10-09. Follow it exactly; never improvise a step, a link or a price.

**Rules.** You never see, ask for, read or repeat the key or a password. The person types their password themselves and pastes the key into Settings › Keys; never accept either in chat. If they paste a key in chat anyway, post `Please don't paste keys here. Delete that message, then put the key in Settings › Keys.` and carry on. Browser steps follow `references/browser-person-steps.md`. When `web_search_ready` is true, search already works; skip this key setup. Every install tries search without a key first; a Brave key can make search steadier. When `web_search_ready` is false, search didn't work on the check; a Brave key is an optional alternative.

## 1. Explain, then ask

One `post_update`, word for word. Keep the first and last bracketed parts only when step 5 of `references/first-run-app.md` will run the lookup (the app path); on any other path leave both out. Keep the middle bracketed part only when `## Setup facts` shows `web_search_ready` false; on a path without that fact, leave it out.

> Web search lets me look things up on the web for you[, starting with you, so you don't have to tell me everything yourself]. [I couldn't search without a key just now. ]A Brave Search key makes search steadier. Brave's free plan still asks for a credit card; search costs $5 per 1,000 searches, and every month includes $5 of free credit, enough for 1,000 searches. That's usually enough for most people, so search usually costs nothing. Searches using that key go to Brave. Do you already have a Brave Search API account? It's the account apps use to search, separate from the Brave browser. [If you'd rather I didn't look you up, just say so.]

End your turn. Not now: `OK. Settings › Keys has it whenever you want it.` Close their task and task `Connect web search` with `You chose not now. Settings › Keys has it.`

## 2. Open the right page

Open it with `browser_open`; the person watches in the shared browser, so do not set `show_window`. Tell the person in one `post_update` that the pane opened, and fill only the email they chose at install (the saved email; if none is saved, ask for one in chat first).

| They have an account | Open | Fill |
|---|---|---|
| Yes | `https://api-dashboard.search.brave.com/login` | Email |
| No | [Brave Search API sign-up](https://api-dashboard.search.brave.com/register) | Email |

Sign-up asks only for an email and a password; **Create account** stays disabled until the password is filled, and Brave emails a link to confirm the account. Tell them: type your password yourself, press **Create account**, then click the link in the email Brave sends you. These are sign-in pages, so watch per `browser-person-steps.md` B, reading only the URL and title; a captcha with no password field is watched per A.

## 3. Take them to the key

Open [API Keys](https://api-dashboard.search.brave.com/app/keys). A page that sends them back to the login page is theirs (B). If they have no plan yet, they open [Plans](https://api-dashboard.search.brave.com/app/subscriptions/subscribe), choose **Search** and add their card; that is theirs too. When the API Keys page is showing, post once: `The key goes only in Settings › Keys, never here in chat. Press Add API Key, name it CORSIA and copy the key. Then open [Settings › Keys](/settings/keys), paste it under Web search, enter your CORSIA password and press Save. Check tells you right away whether it works. The key stays on this computer; agents use it but can never see it. Tell me when it's in.` Read the URL and title only; never read the key off the page. Add their task `Add your web search key`; task `Connect web search` depends on it. End your turn.

## 4. Help with a step

If the person says a step doesn't match what they see, answer only from this list, then ask them to tell you when the key is in:

- No confirmation email: `Check your spam folder. If it isn't there, sign up again with the same email.`
- Plans page asks them to log in: `Log in with the email and password you just created, then open Plans again.`
- Unsure which plan: `Choose Search. It's the one with web results; the other plans aren't needed.`
- Worried about cost: `Search costs $5 per 1,000 searches, and the $5 of free credit each month covers 1,000. You only pay past that.`
- Can't find where to paste it: `Settings › Keys, under Web search: [Settings › Keys](/settings/keys).`

Anything else: `Tell me what's on your screen and I'll help.`

## 5. After they say it's in

When they say it's in, or on any later wake, first call `corsia_api` with `GET /api/keys`. In `keys`, find the entry with `name` `web-search` and read `is_set`. If false, use the No key found row without searching. If true, run one `web_search` for `CORSIA` and use the matching result row below. If the read fails, or the entry or `is_set` is missing, use the Key check failed row.

| Then | Say | Record |
|---|---|---|
| Results, or none | `Web search works.` | `mark_done` their task with evidence in plain words (what they said, what you checked); task `Connect web search` done |
| `Web search: key refused.` | `Brave didn't accept that key. Copy it again from API Keys and save it in Settings › Keys; Check tells you right away.` | Replace their task with `Add a key Brave accepts`; close the old one (`Key refused`) |
| `Web search failed: [reason class]` | `Web search didn't work just now: [reason, never the key]. Your key is saved; I'll try again next time I'm working.` Once per reason | Note `I'll try again (told you: [reason class])`; later wakes re-check silently |
| No key found | `I don't see it in Settings › Keys yet. Paste it under Web search and press Save.` | Their task stays open |
| Key check failed | `I couldn't check Settings › Keys just now. I'll check again next time I'm working.` Once | Their task stays open; later wakes re-check silently |

A tool result starting `Error:` is a failed search; report it without guessing a reason. You may add a key entry and read entries' metadata, nothing more.
