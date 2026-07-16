---
name: minds-api
description: "Use to act on OTHER Minds workspaces on the user's behalf -- list them, create a fresh one, SSH into one, read or export its backups, start/stop/destroy/recover it. The headline workflow is migrating content out of an old/broken workspace into a new one and then handing the cleanup to the new workspace's own agent. Reached through the latchkey gateway's minds-api-proxy; most routes need a per-workspace permission grant."
compatibility: Requires latchkey (the standard agent gateway) and curl; ssh/ssh-keygen for the SSH capability. mngr (vendored) for handing tasks to another workspace's agent.
---

# Minds API

Minds exposes a small HTTP API that lets an agent in one workspace act on *other*
workspaces through the hub: list them, read detail/version/backups, create new
ones, destroy/start/stop them, export backups, establish SSH access, update
settings, and run health/restart recovery.

You never hold a token. Every call goes through the **latchkey gateway's
`minds-api-proxy`** on the reserved gateway-self host `latchkey-self.invalid`;
the gateway injects the central Minds API key and forwards to the desktop
client. So always use **`latchkey curl`** (not plain `curl`), and address the
proxy like this:

```bash
# The OpenAPI schema (always allowed, no grant needed):
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/schema

# Any /api/v1 route (the proxy strips /minds-api-proxy before forwarding):
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces
```

## Discover the API first

`GET /api/schema` is the **authoritative, always-current** description of every
route you can reach and every request/response type. Read it before assuming a
route or field shape -- this skill lists the highlights, but the schema is the
source of truth (and only lists routes actually reachable through the gateway):

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/schema | jq '.paths | keys'
# Inspect one route's request/response models:
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/schema \
  | jq '.paths["/api/v1/workspaces/{agent_id}/ssh"]'
```

A workspace is addressed by its **agent id** (the `agent_id` field in the
listing below). Your own workspace's id is usually `$MNGR_AGENT_ID`; confirm it
appears in `GET /api/v1/workspaces` if you need to self-reference.

## Getting access for a specific workspace (latchkey permissions)

Only the schema endpoint is allowed by default. Every `/api/v1/workspaces/...`
call is gated by the `minds-workspaces` detent scope, with one permission per
verb. The **targeted** verbs are granted *per workspace*, so you ask for access
to one specific workspace at a time.

| Verb permission | Covers | Targeted? |
|---|---|---|
| `minds-workspaces-read` | list, detail, version, backups (read) | no (all workspaces) |
| `minds-workspaces-create` | create a new workspace | no |
| `minds-workspaces-ssh` | establish SSH access | yes |
| `minds-workspaces-backups-export` | export a backup snapshot | yes |
| `minds-workspaces-destroy` | destroy a workspace | yes |
| `minds-workspaces-lifecycle` | start / stop the host | yes |
| `minds-workspaces-update` | update settings (color, account) | yes |
| `minds-workspaces-recover` | health check + restart | yes |
| `minds-workspaces-sharing` | view/change service sharing | yes |

When a call comes back rejected (a "not permitted by the user" message / 403),
file a permission request and wait for the user to approve it. Minds uses a
dedicated `type: "workspace"` request (distinct from the predefined-service
requests in the `latchkey` skill):

```bash
# (Never pipe the output through jq because frontend rendering depends on seeing the full output from your tool.)
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{
        "agent_id": "'"$MNGR_AGENT_ID"'",
        "type": "workspace",
        "payload": {
          "permissions": ["minds-workspaces-ssh", "minds-workspaces-backups-export"],
          "target_workspace_id": "<TARGET_WORKSPACE_AGENT_ID>"
        },
        "rationale": "I need SSH + backup-export access to <name> so I can migrate its content into a fresh workspace."
      }'
```

- `payload.permissions` is the list of verb names from the table above.
- `payload.target_workspace_id` is the specific workspace the **targeted** verbs
  act on. Omit it / set `null` for the non-targeted verbs (`read`, `create`), or
  to request a verb across *all* workspaces.
- After posting, **wait for a system message** telling you whether the user
  approved or denied (same as the `latchkey` skill's permission flow). Re-run
  your call once approved.

Tip: request exactly the verbs the task needs, with a clear rationale -- the
user sees these as checkboxes per workspace.

## Core capabilities

Paths below are relative to `http://latchkey-self.invalid/minds-api-proxy`.
Bodies are JSON; send `-H 'Content-Type: application/json'` (the API validates
the body only when that header is set).

### List / inspect workspaces (`minds-workspaces-read`)

```bash
latchkey curl .../api/v1/workspaces | jq '.workspaces[] | {agent_id, name, host_state, provider_name}'
latchkey curl .../api/v1/workspaces/<id>            # one workspace's detail
latchkey curl .../api/v1/workspaces/<id>/version    # minds version + upgrade history
```

The listing includes destroyed-but-still-backed-up workspaces, so you can find
an old workspace even after its host is gone.

### Create a new workspace (`minds-workspaces-create`)

`POST /api/v1/workspaces` returns `202` with an operation handle; the new
workspace's `agent_id` appears once `mngr create` finishes, so you **poll the
typed operation route** until it's done:

```bash
OP=$(latchkey curl -XPOST .../api/v1/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"git_url": "<template-repo-url>"}' | jq -r .operation_id)

# Poll create status (DONE -> the workspace is ready; FAILED -> read .error):
latchkey curl .../api/v1/workspaces/operations/create/$OP | jq '{status, is_done, agent_id, error}'
# Live logs (server-sent events):
latchkey curl -N .../api/v1/workspaces/operations/create/$OP/logs
```

`git_url` is required (typically the template repo a fresh mind is built from).
Many optional fields exist (`host_name`, `branch`, `launch_mode`, `ai_provider`,
`account_id`, `region`, `backup_*`) -- see `CreateWorkspaceRequest` in the
schema. A `400` with `{error, field}` means a field-level problem; a `422`
`{"errors":[{field,message}]}` means a structurally invalid body.

**Backups: always create with backups unconfigured.** Leave every `backup_*`
field unset (the default is `backup_provider=CONFIGURE_LATER`). Configuring
backups can involve the user's storage credentials (`backup_api_key_env`),
which are secrets you must NEVER ask the user for and never send through this
API -- the user enables backups themselves from the minds desktop app
afterwards. (The user's *master password* is likewise never yours to handle;
newer minds versions use it only inside the desktop app to protect
cross-device sync, and it does not appear in this API at all.)

### SSH into another workspace (`minds-workspaces-ssh`)

You generate a keypair locally; only the public key leaves you, and the grant is
time-limited. Pass your own workspace id as `requester_workspace_id` (the hub
needs it to dedupe your grant and, for a *local* target, to broker a tunnel back
into your container).

```bash
ssh-keygen -t ed25519 -N '' -f /tmp/mind_key   # /tmp/mind_key(.pub)

CONN=$(latchkey curl -XPOST .../api/v1/workspaces/<TARGET_ID>/ssh \
  -H 'Content-Type: application/json' \
  -d '{"public_key": "'"$(cat /tmp/mind_key.pub)"'", "requester_workspace_id": "'"$MNGR_AGENT_ID"'"}')
echo "$CONN" | jq    # {agent_id, user, host, port, expires_at}

ssh -i /tmp/mind_key -p "$(echo "$CONN" | jq -r .port)" \
    "$(echo "$CONN" | jq -r .user)@$(echo "$CONN" | jq -r .host)"
```

- For a **remote** target you get its real address. For a **local**
  (Docker/Lima) target you get `host="127.0.0.1"` and a port that is reachable
  from inside *your own* workspace (the hub reverse-tunnels into your container),
  so run the `ssh` from this workspace.
- The grant expires at `expires_at`; re-request to refresh (it won't stack).
- `502` usually means the target (or your own container) is offline/unreachable;
  `404` means the workspace id is unknown.

### Read / export backups (`minds-workspaces-read`, `minds-workspaces-backups-export`)

```bash
# List snapshots (works even for an offline/destroyed workspace):
latchkey curl .../api/v1/workspaces/<id>/backups \
  | jq '{is_backing_up, snapshots: [.snapshots[] | {short_id, time, total_size_bytes}]}'

# Export one snapshot as a zip (binary stream -> save with -o):
latchkey curl -o /tmp/restore.zip \
  -XPOST .../api/v1/workspaces/<id>/backups/<snapshot_id>/export
```

### Recover / lifecycle (`minds-workspaces-recover`, `-lifecycle`, `-destroy`)

```bash
latchkey curl .../api/v1/workspaces/<id>/health | jq           # probes + dispatch tier
latchkey curl -XPOST .../api/v1/workspaces/<id>/restart -H 'Content-Type: application/json' -d '{"scope":"services"}'
latchkey curl -XPOST .../api/v1/workspaces/<id>/start          # or /stop
latchkey curl -XPOST .../api/v1/workspaces/<id>/destroy        # 202 + poll operations/destroy/<id>
```

Restart and destroy return operation handles; poll
`.../api/v1/workspaces/operations/restart/<id>` and
`.../api/v1/workspaces/operations/destroy/<id>` (each with `/logs`) the same way
as create.

## Headline workflow: migrate an old workspace into a fresh one

This strings the capabilities together. Suppose the user has an old, outdated or
broken workspace and wants its content moved into a clean new one.

1. **Find the old workspace.** `GET /api/v1/workspaces`; note its `agent_id`
   (call it `OLD`). (Needs `minds-workspaces-read`.)

2. **Create the fresh workspace.** `POST /api/v1/workspaces` with the template
   `git_url`; poll `operations/create/<op>` until `status == "DONE"`, then read
   `agent_id` (call it `NEW`). (Needs `minds-workspaces-create`.)

3. **Get the old content out.** Either:
   - export the latest backup: `GET .../<OLD>/backups`, pick the newest
     `snapshot_id`, `POST .../<OLD>/backups/<snapshot_id>/export -o /tmp/old.zip`
     (`minds-workspaces-backups-export`); **or**
   - SSH into `OLD` (`minds-workspaces-ssh`) and pull files directly
     (`tar`/`rsync` over the SSH connection from step "SSH into another
     workspace").

4. **Put the content into the new workspace.** SSH into `NEW`
   (`minds-workspaces-ssh`) and copy the content in (unzip `/tmp/old.zip`, or
   rsync from your pulled copy). Place files where the new workspace expects them
   (typically the project working directory).

5. **Hand the cleanup to the new workspace's own agent.** You now have a shell on
   `NEW`. You can fix things directly, but for anything open-ended it's better to
   let `NEW`'s own agent take over -- it knows that workspace. From your SSH
   session on `NEW`:

   ```bash
   mngr list                       # find NEW's own agent id on this host
   mngr message <new-agent-id> "I migrated content from an older workspace into your project dir. Please review it, fix anything broken (deps, configs, services), and get the workspace healthy."
   ```

   The agent there picks up the message and self-repairs. (If `mngr` isn't on the
   path in that workspace, just do the repairs yourself over the SSH session.)

Throughout: request only the per-workspace permissions each step needs, with a
rationale that names the workspace and the goal, and wait for approval before
retrying a gated call.

## Notes

- Always prefer `latchkey curl` over `curl`; the gateway is what injects auth and
  routes the proxy. A connection error (curl exit 7) usually means the user's
  computer is offline.
- The schema endpoint is the contract. If a field or route here ever disagrees
  with `GET /api/schema`, trust the schema.
- Don't expose the proxy/gateway mechanics to the user unless they ask -- talk in
  terms of "your workspaces".
- For general latchkey usage and the predefined-service permission flow, see the
  `latchkey` skill; this skill only adds the Minds-specific `type: "workspace"`
  permission request and the workspace routes.
