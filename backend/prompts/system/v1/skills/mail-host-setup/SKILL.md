---
name: mail-host-setup
description: Use when email or calendar needs connecting and the host is unknown or not Gmail or Microsoft (iCloud, Yahoo), a row needs an app password, or a System-change notice adds this skill.
---

# Connect another email host

Only the designated CORSIA thread runs this. Never ask for, type, read, print or paste a password. Never open the host's app-password page with `browser_open`. If the person pastes a password into chat, never repeat it; tell them to delete that app password on their host's site and make a new one.

1. If the address or host is Microsoft (Outlook.com, Hotmail, Live, Microsoft 365), stop this skill and follow `microsoft-setup`. If Gmail or Google Workspace, stop this skill, load pack `email`, call `google_status`, then `google_connect` and post its link: one Allow, no Google Cloud project. Follow `needs_action` for an existing account. Use skill `google-setup` only when the person says Google blocked the sign-in (for example "Access blocked" or "your admin hasn't allowed this app"), when the same address comes back refused after they said they clicked Allow, or when they ask for their own Google Cloud project. An expired link gets a new link; a Cancel or "not now" is a decline, not a refusal.
2. If you don't have their email address, ask: `What's the email address?` Then apply step 1 to it.
3. Post: `I can look after your <host> mail and calendar. <host> needs an app password for that, made on <host>'s site. I'll show you where; you paste it into Settings, never into chat. Go ahead?` Not now: `Ask me any time to connect your email.` and stop; nothing is added until they say yes.
4. Load pack `email` with `tools_load`, then call `mail_host_connect` with the address. If its `status` is `ok` or `mail_only`, this address is already connected: post `Connected as <address>.` and stop. If it returns an account that is waiting for its app password, carry on from step 5 using what it returned. If it says it doesn't know the address's mail servers, ask once: `I couldn't find <domain>'s mail servers. Your IT or host's help pages list them: the IMAP server, the SMTP server, and the calendar (CalDAV) address if there is one.` Call it again with them. If that host has no app passwords, the person sets the account password the same way, with Set key on its row in Connected apps, never in chat. For any other existing status, post what the person does next as its `next` says and stop.
5. Post the `app_password_url` it returns as a link, with its `click_path`, plus its `first_turn_on_imap` step if any (switch on IMAP, two-factor sign-in). Then: `When you've made it, press Set key on the <host> row in Settings › Connected apps and paste it. Tell me when it's in.`
6. When they say it's in, call `mail_host_connect` with the same address again; it changes nothing and returns the account's current `status`. Post `Connected as <address>.` for `ok` or `mail_only`, or the named failure and what to do for any other status (`refused`: make a new app password and press Replace key; `imap_off`: turn on IMAP at the host, then press Retry; `unreachable`: check the server names, then press Edit server or Retry; `checking`: it is still being checked, so wait a moment and look again).

## After an update

When a System-change notice adds this skill: if the profile says `Email app: Other`, offer once (step 3, after asking for the address), then go on from step 4; record the notice `deferred` while the offer is open, `acted` once the account is added, or `not_applicable` if they say not now, and never offer again. Otherwise record it `not_applicable`.
