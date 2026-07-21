Eval worker for the minds-evals harness (no-ops unless `scripts/test_case_metadata.json` is present, so normal workspaces are unaffected).

The worker drives a multi-turn conversation from the case's `prompts` array (one entry per turn), snapshots `/mngr` to S3 with restic each turn, and uploads the full transcript at the end -- so a launched run self-completes and results are retrievable from S3.

Each prompt entry is either a literal string (sent to the agent verbatim) or the sentinel `DECIDE_FROM_PERSONA`, which makes the worker role-play the client: it feeds the transcript-so-far plus the case persona to the Anthropic API and sends back a short casual reply (falls back to "Sounds good." if the call fails, so a flaky API never stalls the run). All credentials (AWS, restic, and the Anthropic key for the role-play) come from the slotted metadata file.

The sink now writes to Cloudflare R2 (S3-compatible) rather than AWS S3: the boto3 client uses the `s3_endpoint` from the case metadata (restic already reaches R2 via the endpoint baked into `restic_repository`), and region defaults to `auto`. Credentials are still the scoped key from the slotted metadata file.
