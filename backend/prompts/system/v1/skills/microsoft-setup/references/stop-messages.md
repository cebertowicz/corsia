# Microsoft setup: stop messages

Post each exactly, filling only the angle-bracket parts. Where a message for IT follows, post it as its own ```` ```text ```` block so the person can copy it.

## R1 Registration disabled (step 6)

`Your organization doesn’t let you register apps. Send this to your IT admin, then ask me to connect Outlook again.`

Message for IT: `Please let me register applications in Microsoft Entra ID, either by allowing users to register applications or by giving my account, <address>, the Application Developer role. I want to register my own app named CORSIA so it can access my own mail, calendar and files.`

## R2 Existing app takes other account types (step 7)

`Your CORSIA app on Microsoft only accepts <its supported account types>, and Microsoft doesn’t let that change, so I can’t connect <address> with it. I’ve changed nothing. I can connect an account of that type now if you have one. This version of CORSIA can’t set up a second app for this account.`

## R3 Needs admin approval: `admin_approval_required` (step 14)

`Your organization needs to approve CORSIA before it can connect this account. Send this to your IT admin, then ask me to connect Outlook again.`

Message for IT: `Please approve my CORSIA app for my account, <address>. It is my own Microsoft app registration, Application (client) ID <client ID from step 11>, and requests delegated Microsoft Graph access to Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, Files.ReadWrite, offline_access and User.Read.`

## R4 Sign-in cancelled: `consent_cancelled` (step 14)

`Sign-in was cancelled. Ask me any time to try again.` Nothing else.
