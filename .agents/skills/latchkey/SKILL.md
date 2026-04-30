---
name: latchkey
description: Interact with third-party or self-hosted services (Slack, Google Workspace, Dropbox, GitHub, Linear, Coolify...) using their HTTP APIs on the user's behalf.
compatibility: Requires node.js, curl and latchkey (npm install -g latchkey). A desktop/GUI environment is required for the browser functionality.
---

# Latchkey

## Instructions

Latchkey is a CLI tool that automatically injects credentials into curl commands. Credentials (mostly API tokens) can be either manually managed or, for some services, Latchkey can open a browser login pop-up window and extract API credentials from the session.

Use this skill when the user asks you to work on their behalf with services that have HTTP APIs, like AWS, GitLab, Google Drive, Discord or others.

Usage:

1. **Use `latchkey curl`** instead of regular `curl` for supported services.
2. **Pass through all regular curl arguments** - latchkey is a transparent wrapper.
3. **Check for `latchkey services list`** to get a list of supported services. Use `--viable` to only show the currently configured ones.
4. **Use `latchkey services info <service_name>`** to get information about a specific service (auth options, credentials status, API docs links, special requirements, etc.).
5. **Submit a permission request to the user if necessary** by calling `curl -XPOST http://localhost:8000/api/permissions/request` when either there are no credentials for the given service or the curl requests come back with the "request not permitted by the user" message.
6. **Look for the newest documentation of the desired public API online.** Avoid bot-only endpoints.


## Examples

### Make an authenticated curl request
```bash
latchkey curl [curl arguments]
```

### Creating a Slack channel
```bash
latchkey curl -X POST 'https://slack.com/api/conversations.create' \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-channel"}'
```

(Notice that `-H 'Authorization: Bearer` is not present in the invocation.)

### Getting Discord user info
```bash
latchkey curl 'https://discord.com/api/v10/users/@me'
```

### Ask for user permission

When either there are no credentials for the given service yet or our
requests come back with the "request not permitted by the user
message", ask the user for permission first:

```bash
curl -XPOST http://localhost:8000/api/permissions/request -d '{"request_type": "LATCHKEY_PERMISSION", "service_name": "discord", "rationale": "I'd like to access your Discord account to read server and channel information so I can help you summarize conversations."}'
```

After that, wait for a system message to see if the user approved or denied the permission request.

### Detect expired credentials and force a new login to Discord
```bash
latchkey services info discord  # Check the "credentialStatus" field - shows "invalid"
latchkey auth browser discord
latchkey curl 'https://discord.com/api/v10/users/@me'
```

Only do this when you notice that your previous call ended up not being authenticated (HTTP 401 or 403).

### List usable services

```bash
latchkey services list --viable
```

Lists services that either have stored credentials or can be authenticated via a browser.

### Get service-specific info
```bash
latchkey services info slack
```

Returns auth options, credentials status, and developer notes
about the service. If `browser` is not present in the
`authOptions` field, the service requires the user to directly
set API credentials via `latchkey auth set` or `latchkey auth
set-nocurl` before making requests.


## Storing credentials

Aside from the `latchkey auth browser` case, it is the user's responsibility to supply credentials.
The user would typically do something like this:

```bash
latchkey auth set my-gitlab-instance -H "PRIVATE-TOKEN: <token>"
```

When credentials cannot be expressed as static curl arguments, the user would use the `set-nocurl` subcommand. For example:

```bash
latchkey auth set-nocurl aws <access-key-id> <secret-access-key>
```

If a service doesn't appear with the `--viable` flag, it may
still be supported; the user just hasn't provided the
credentials yet. `latchkey service info <service_name>` can be
used to see how to provide credentials for a specific service.


## Notes

- All curl arguments are passed through unchanged
- Return code, stdout and stderr are passed back from curl
- Credentials are always stored encrypted and are never transmitted anywhere beyond the endpoints specified by the actual curl calls.

## Currently supported services

Latchkey currently offers varying levels of support for the
following services: AWS, Calendly, Coolify, Discord, Dropbox, Figma, GitHub, GitLab,
Gmail, Google Analytics, Google Calendar, Google Docs, Google Drive, Google Sheets,
Linear, Mailchimp, Notion, Sentry, Slack, Stripe, Telegram, Umami, Yelp, Zoom, and more.

### User-registered services

Note for humans: users can also add limited support for new services
at runtime using the `latchkey services register` command.
