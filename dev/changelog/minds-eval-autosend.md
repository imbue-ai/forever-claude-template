Eval worker for the minds-evals harness (no-ops unless `scripts/test_case_metadata.json` is present, so normal workspaces are unaffected).

The worker drives a multi-turn conversation from the case's `prompts` array (one entry per turn), snapshots `/mngr` to S3 with restic each turn, and uploads the full transcript at the end -- so a launched run self-completes and results are retrievable from S3.

Each prompt entry is either a literal string (sent to the agent verbatim) or the sentinel `DECIDE_FROM_PERSONA`, which makes the worker role-play the client: it feeds the transcript-so-far plus the case persona to the Anthropic API and sends back a short casual reply (falls back to "Sounds good." if the call fails, so a flaky API never stalls the run). All credentials (AWS, restic, and the Anthropic key for the role-play) come from the slotted metadata file.

The sink now writes to Cloudflare R2 (S3-compatible) rather than AWS S3: the boto3 client uses the `s3_endpoint` from the case metadata (restic already reaches R2 via the endpoint baked into `restic_repository`), and region defaults to `auto`. Credentials are still the scoped key from the slotted metadata file.

Renamed the eval worker's `eval_aws_sink.py` / `AwsSink` to `eval_sink.py` / `EvalSink` -- results go to R2 (S3-compatible), so the AWS-specific naming no longer fit.

Reverted the global `[create_templates.modal]` sandbox timeout to 24h (86310s) and added a `[create_templates.modal_eval]` overlay (3h) that eval workers stack on top -- so eval-specific timeouts no longer alter the shared Modal template. The role-play decider gets its Anthropic key from the harness-written `test_case_metadata.json` (with `ANTHROPIC_API_KEY` in the env as a local-testing fallback), a harness-controlled source independent of the workspace agent's own auth.
