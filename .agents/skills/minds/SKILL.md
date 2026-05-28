---
name: minds
description: Use to create new minds and fetch the status and logs of existing minds.
---

# Minds

## Instructions

First, use the `latchkey` skill to request permission from the user for the "minds" service. Then, use `latchkey services info minds` to retrieve the base API URL, and `latchkey curl` for all minds-related requests.

### Create a mind

Create a mind using the `POST /api/create-agent` endpoint. Notable JSON fields include:

- `host_name`: Name of the mind. Make this descriptive.
- `git_url`: Git repository URL to clone for the mind; can use "https://github.com/imbue-ai/forever-claude-template.git" in most cases.

This returns a response in the form `{"agent_id": "creation-79f8dc3948d6474fb42564125f027e5c", "status": "CLONING"}`; the `agent_id` can then be used to track the progress of the mind creation using `GET /api/create-agent/{agent_id}/status`.
