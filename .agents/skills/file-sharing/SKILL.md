---
name: file-sharing
description: Use to read and write files and directories on the user's local filesystem.
---

# File sharing

## Instructions

Use this skill when the user asks you to work with files located directly on their PC.

1. **Use `latchkey curl`** calls to communicate with the remote WebDAV server. (They are the same as normal curl calls, just going through the Latchkey Gateway.)
2. **Submit a permission request to the user** by calling `latchkey curl -XPOST http://latchkey-self.invalid/permission-requests` when the curl request comes back with the "request not permitted by the user" message. See the "Ask for user permission" example below.
3. **Stop working** in case of upstream connection failures. Those are most likely caused by the user closing their locally running Minds app. Restarting the Minds app should usually help.

The base URL is `http://latchkey-self.invalid/minds-api-proxy/api/v1/files`. Only the user's home directory and the user's system temp directory are accessible. Use the `$MINDS_API_KEY` env var for authentication (only when accessing the /minds-api-proxy endpoints).
MOVE and COPY operations are not supported.


## Examples

### Retrieving a file
```bash
latchkey curl -H "Authorization: Bearer $MINDS_API_KEY" -O http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/notes.txt
```

### Writing a file
```bash
latchkey curl -H "Authorization: Bearer $MINDS_API_KEY" -T localfile.txt http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/remotefile.txt
```

### Listing a directory
```bash
latchkey curl -H "Authorization: Bearer $MINDS_API_KEY" -s -X PROPFIND -H "Depth: 1" http://latchkey-self.invalid/minds-api-proxy/api/v1/files/home/hynek/project/ | xmlstarlet sel -N d=DAV: -t -m "//d:response/d:href" -v . -n
```

### Ask for user permission

When a request comes back with the "request not permitted by the user"
message, ask the user for permission first:

```bash
# 1. Retrieve the list of your existing permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules

# 2. Ask for the necessary missing permissions.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "file-sharing", "payload": {"path": "/home/hynek/project", "access": "READ"}, "rationale": "I'"'"'d like to access the /home/hynek/project directory in order to find the most recent accounting spreadsheet you asked me about."}'
```

The body must be a JSON object with exactly four fields:
`agent_id` (use `$MNGR_AGENT_ID`), `rationale`, `type` (use "file-sharing"), and `payload`.

`payload` must be an object with exactly two string fields: `path` and `access`. `path` must be absolute, `access` must be "READ" or "WRITE".

After posting, wait for a system message indicating whether the user approved or denied the permission request.
