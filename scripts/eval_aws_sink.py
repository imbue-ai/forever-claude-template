"""S3 sink for the eval worker.

Self-contained: all credentials come from the slotted scripts/test_case_metadata.json (aws_access_key_id /
aws_secret_access_key / restic_repository / restic_password), NOT from minds' backup provisioning
(which does not reliably land a restic.env inside a Modal sandbox). We drive restic ourselves:

- restic snapshots of /mngr, tagged post_message_<k>, at the turns we choose.
- plain S3 objects (state.json, transcript) via boto3, same creds, into the case's S3 prefix.
"""

from __future__ import annotations

import json
import os
import subprocess

HOST_DIR = os.environ.get("MNGR_HOST_DIR", "/mngr")

# Match host-backup's exclude set so snapshots stay lean (deps are reinstallable from lockfiles).
_RESTIC_EXCLUDES = (
    "--exclude=**/.venv", "--exclude=**/node_modules", "--exclude=**/__pycache__",
    "--exclude=**/.pytest_cache", "--exclude=**/.ruff_cache", "--exclude=**/target",
    "--exclude=**/dist", "--exclude=**/build", "--exclude=**/.next", "--exclude=**/.cache",
)


class AwsSink:
    def __init__(self, config: dict):
        self._config = config
        self._bucket = config["s3_bucket"]
        self._prefix = str(config["s3_prefix"]).rstrip("/")
        self._region = config.get("aws_region", "us-east-1")
        self._key = config.get("aws_access_key_id", "")
        self._secret = config.get("aws_secret_access_key", "")
        self._repo = config.get("restic_repository", "")
        self._restic_password = config.get("restic_password", "")
        import boto3

        self._s3 = boto3.client(
            "s3", aws_access_key_id=self._key, aws_secret_access_key=self._secret, region_name=self._region,
        )

    def _restic_env(self) -> dict:
        return {
            **os.environ,
            "RESTIC_REPOSITORY": self._repo, "RESTIC_PASSWORD": self._restic_password,
            "AWS_ACCESS_KEY_ID": self._key, "AWS_SECRET_ACCESS_KEY": self._secret,
            "AWS_DEFAULT_REGION": self._region,
        }

    def restic_snapshot(self, tag: str) -> None:
        if not (self._repo and self._restic_password):
            print("[eval] no restic repo/password in config -- skipping snapshot", tag, flush=True)
            return
        env = self._restic_env()
        if subprocess.run(["restic", "cat", "config"], env=env, capture_output=True, text=True).returncode != 0:
            init = subprocess.run(["restic", "init"], env=env, capture_output=True, text=True)
            if init.returncode != 0:
                print("[eval] restic init failed: {}".format((init.stderr or "").strip()[:300]), flush=True)
        result = subprocess.run(
            ["restic", "backup", HOST_DIR, "--tag", tag, *_RESTIC_EXCLUDES],
            env=env, capture_output=True, text=True,
        )
        print("[eval] restic snapshot {} rc={} {}".format(
            tag, result.returncode, (result.stderr or "").strip()[:200] if result.returncode else ""), flush=True)

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key="{}/{}".format(self._prefix, key), Body=data, ContentType=content_type)

    def write_state(self, waits_done: int, num_turns: int, test_state: str) -> None:
        payload = {
            "eval_name": self._config.get("eval_name"),
            "case_name": self._config.get("case_name"),
            "waits_done": waits_done,
            "num_turns": num_turns,
            "test_state": test_state,
        }
        self._put("state.json", json.dumps(payload, indent=2).encode("utf-8"), "application/json")

    def upload_transcript(self, events: list[dict]) -> None:
        body = "\n".join(json.dumps(event) for event in events).encode("utf-8")
        self._put("artifacts/full_transcript.jsonl", body, "application/x-ndjson")
