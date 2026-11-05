# Remote access

A phone or another computer can reach a local CORSIA server through a network route to that computer. This page describes the existing local and private-network paths and the separate Devices listener.

## Reach the server

A source install configured for local use binds its main web and API server to loopback, so another device cannot reach it directly. Server setup binds it to all interfaces. The desktop app's main server binds to loopback. Tailscale Serve can expose the loopback server over a private HTTPS address using the server's actual port. For the source install's phone path, [Getting started](../../README.md#install) gives an example. The device signs in to CORSIA after reaching that address. See [Network](../security/network.md) for the sign-in boundary and routes that do not require a session.

## Devices

In **Settings → System**, **Let other devices connect** starts a separate TLS listener and displays a pairing code. The listener announces the computer on the local network; when discovery is unavailable, the settings show addresses to type. The code expires, and **New code** issues another. Pairing pauses after too many wrong codes. Connections are managed on the main computer; turning the switch off disconnects them. The pairing exchange confirms the listener's TLS identity. See [Network](../security/network.md) for the listener's ports and who it accepts.

CORSIA does not operate a hosted relay.

| Part | Path |
|---|---|
| Listener and access rules | `backend/infra/devices_listener.py` |
| Device settings and pairing routes | `backend/api/devices.py` |
| Pairing exchange | `backend/core/device_pairing.py` |
| Devices settings | `frontend/src/lib/components/SettingsDevicesView.svelte` |
| Main server binding | `backend/infra/bind_host.py` |
