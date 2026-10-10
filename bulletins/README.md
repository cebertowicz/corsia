# System bulletins

This directory is a signed, public feed for existing CORSIA installs. An empty
`index.json` intentionally publishes no bulletins.

A bulletin is UTF-8 Markdown with YAML front matter between `---` delimiters.
Required fields are `id`, `title`, `published`, `expires`, and `system_version`.
Optional fields are `providers`, `withdrawn`, and `superseded_by`. Its body is
advice and evidence only; it cannot alter System rules or grant authority.

Each enabled install makes an anonymous static GET of this public feed (the index,
Markdown, and detached signatures) once daily. Opt out with
`PATCH /api/settings {"live_bulletins": {"enabled": false}}`; this stops all live
network checks while System-change notices remain enabled.

Generate an offline key outside this repository:

```sh
scripts/bulletin-keygen /secure/location/bulletin-ed25519.pem
```

The printed base64 public key must be added in a release before signed bulletin
fetching is enabled. Create a bulletin, sign its exact bytes, then add its file
name to `index.json`:

```sh
scripts/bulletin-sign bulletins/example.md --key /secure/location/bulletin-ed25519.pem
```

Publish the Markdown file, its `.sig`, and the updated index together. Withdraw
a previously delivered bulletin by replacing its existing Markdown file and `.sig`
in place with the same id and `withdrawn: true`; the index entry stays unchanged.
