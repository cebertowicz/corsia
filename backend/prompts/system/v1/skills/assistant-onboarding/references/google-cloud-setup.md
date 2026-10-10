# Google Cloud setup fallback

Use this when headed browser automation is unavailable or cannot perform a named step. Keep the setup plan visible. If browser automation is unavailable, the user completes every numbered Cloud Console step; if it reaches the client page but cannot capture the download, only step 5 needs the user.

1. Create or select a Google Cloud project: <https://console.cloud.google.com/projectcreate>
2. Enable the Gmail, Google Calendar, Google Drive, and Google Docs APIs: <https://console.cloud.google.com/apis/library>
3. Configure the OAuth consent screen for the project: <https://console.cloud.google.com/apis/credentials/consent>
4. Create a Desktop app OAuth client: <https://console.cloud.google.com/apis/credentials>
5. Download that Desktop client JSON. If headed browser automation cannot capture the download, this Download click is the one extra setup action; do not open, paste, print, or upload the file.
6. Source the shipped `gws-env.sh --client-json "<downloaded-client-json-path>"` helper. It moves the file into the thread's own Google Workspace home outside the workspace (named by `CORSIA_GWS_HOME`, readable and writable by this thread's shell only; the helper fails if the variable is missing) with mode 600 and exports its values only inside the current shell.
7. Return to CORSIA for `gws auth login`; choose the account and click Allow.

The agent never reads or prints the client configuration, client secret, refresh token, or credential file. The helper is the only client-config handoff path.
