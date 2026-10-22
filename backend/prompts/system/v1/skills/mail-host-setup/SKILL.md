---
name: mail-host-setup
description: Use when the person wants email or calendar from a host other than Gmail or Microsoft connected (iCloud, Yahoo, Fastmail, AOL, Zoho, GMX, a work or own server), or a mail-host row says it needs an app password.
---

# Connect another email host

Only the designated CORSIA thread runs this. Never ask for, type, read, print or paste a password. Never open the host's app-password page with `browser_open`. If the person pastes a password into chat, never repeat it; tell them to delete that app password on their host's site and make a new one.

1. If the address or host is Microsoft (Outlook.com, Hotmail, Live, Microsoft 365), stop this skill and follow `microsoft-setup`. If Gmail or Google Workspace, stop this skill and follow `google-setup` from its step 2.
2. If you don't have their email address, ask: `What's the email address?` Load pack `google` with `tools_load`, then call `mail_host_connect` with the address. If its `status` is `ok` or `mail_only`, this address is already connected: post `Connected as <address>.` and stop. If it returns an account that is waiting for its app password, carry on from step 3 using what it returned. For any other status, post what the person does next as its `next` says and stop.
3. Post: `I can look after your <host> mail and calendar. <host> needs an app password for that: a password just for CORSIA, made on <host>'s site. I'll show you where; you paste it into Settings, never into chat. Go ahead?` Not now: `Ask me any time to connect your email.`
4. If `mail_host_connect` says it doesn't know the address's mail servers, ask once: `I couldn't find <domain>'s mail servers. Your IT or host's help pages list them: the IMAP server, the SMTP server, and the calendar (CalDAV) address if there is one.` Call it again with them. If that host has no app passwords, the account password goes in the same Keys entry, never in chat.
5. If it returns `cannot_connect` (for example Fastmail Basic), post its text and stop.
6. Post the `app_password_url` it returns as a link, with its `click_path`, plus its `first_turn_on_imap` step if any (switch on IMAP, two-factor sign-in). Then: `When you've made it, press Set key on the <host> row in Settings › Connected apps, paste it and press Set. Tell me when it's in.`
7. When they say it's in, call `mail_host_connect` with the same address again; it changes nothing and returns the account's current `status`. Post `Connected as <address>.` for `ok` or `mail_only`, or the named failure and what to do for any other status (`refused`: make a new app password and press Replace key; `imap_off`: turn on IMAP at the host, then press Try again; `unreachable`: check the server names, then press Edit server or Try again; `checking`: it is still being checked, so wait a moment and look again).
