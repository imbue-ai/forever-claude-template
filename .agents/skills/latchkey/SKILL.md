---
name: latchkey
description: Use whenever you want to use latchkey commands or interact with third-party or self-hosted services (Slack, Google Workspace, Dropbox, GitHub, Linear, Coolify...) using their HTTP APIs on the user's behalf.
compatibility: Requires node.js, curl and latchkey (npm install -g latchkey).
---

# Latchkey

## Instructions

Latchkey is a CLI tool that automatically injects credentials into curl commands. Credentials are managed on the outside by the Minds app - sending a permission request also triggers a login flow if necessary.

Use this skill when the user asks you to work on their behalf with services that have HTTP APIs, like AWS, GitLab, Google Drive, Discord or others.

Usage:

1. **Use `latchkey curl`** instead of regular `curl` for supported services.
2. **Pass through all regular curl arguments** - latchkey is a transparent wrapper.
3. **Check for `latchkey services list`** to get a list of supported services. Use `--viable` to only show the currently configured ones.
4. **Use `latchkey services info <service_name>`** to get information about a specific service (auth options, credentials status, API docs links, special requirements, etc.).
5. **Submit a permission request to the user if necessary** by calling `latchkey curl -XPOST http://latchkey-self.invalid/permission-requests` (see the "Ask for user permission" example below) when either there are no valid credentials for the given service or the curl requests come back with the "request not permitted by the user" message. (Note: the path is `/permission-requests`, NOT `/extensions/permission-requests`; the `/extensions/*` namespace is admin-only and returns 403 to agents.)
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

When either there are no valid credentials for the given service or our
requests come back with the "request not permitted by the user"
message, ask the user for permission. The requests are sent to
Latchkey via the reserved `latchkey-self.invalid` host:

```bash
# 1. Retrieve the list of available permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/available/discord

# 2. Retrieve the list of your existing permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules

# 3. Ask for the necessary missing permissions.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "discord-api", "permissions": ["discord-read-all"]}, "rationale": "I'"'"'d like to access your Discord account to read server and channel information so I can help you summarize conversations."}'
```

The body must be a JSON object with exactly four fields:
`agent_id` (use `$MNGR_AGENT_ID`), `type` (use "predefined"), `payload`, and `rationale`.

`payload` must be an object with exactly two fields: `scope` (string) and `permissions` (array of strings). `scope` needs to be the scope specified in the response to the `/permissions/available/<service_name>` call.

When not sure (and if applicable), prefer the `*-read-all` permission variants as they are relatively safe and obvious.

After posting, wait for a system message indicating whether the user
approved or denied the permission request.


### List usable services

```bash
latchkey services list --viable
```

Lists services that either have stored credentials or can be easily authenticated into via a browser.

### Get service-specific info
```bash
latchkey services info slack
```

Returns auth options, credentials status, and developer notes about the service.


## Notes

- All curl arguments are passed through unchanged
- Return code, stdout and stderr are passed back from curl

## Currently supported services

Latchkey currently offers varying levels of support for the
following services: AWS, Calendly, Coolify, Discord, Dropbox, Figma, GitHub, GitLab,
Gmail, Google Analytics, Google Calendar, Google Docs, Google Drive, Google Sheets,
Linear, Mailchimp, Notion, Sentry, Slack, Stripe, Telegram, Umami, Yelp, Zoom, and more.
