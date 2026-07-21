"""R2 (S3-compatible) sink for the eval worker.

Self-contained: all credentials come from the slotted scripts/test_case_metadata.json (aws_access_key_id /
aws_secret_access_key / s3_endpoint / restic_repository / restic_password), NOT from minds' backup
provisioning (which does not reliably land a restic.env inside a Modal sandbox). We drive restic ourselves:

- restic snapshots of /mngr, tagged post_message_<k>, at the turns we choose.
- plain objects (state.json, transcript) via boto3 against the R2 endpoint, same creds, into the case's prefix.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time

HOST_DIR = os.environ.get("MNGR_HOST_DIR", "/mngr")
# Default wall-clock budget for one case; the run self-terminates (state -> timed_out) past this.
# Overridable via test_case_metadata.json "timeout_seconds". Stays under the 3h sandbox cap.
DEFAULT_TIMEOUT_SECONDS = 3600.0


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


# Match host-backup's exclude set so snapshots stay lean (deps are reinstallable from lockfiles).
_RESTIC_EXCLUDES = (
    "--exclude=**/.venv",
    "--exclude=**/node_modules",
    "--exclude=**/__pycache__",
    "--exclude=**/.pytest_cache",
    "--exclude=**/.ruff_cache",
    "--exclude=**/target",
    "--exclude=**/dist",
    "--exclude=**/build",
    "--exclude=**/.next",
    "--exclude=**/.cache",
)


class AwsSink:
    def __init__(self, config: dict):
        self._config = config
        self._bucket = config["s3_bucket"]
        self._prefix = str(config["s3_prefix"]).rstrip("/")
        self._region = config.get("aws_region") or "auto"
        self._endpoint = config.get("s3_endpoint", "")
        self._key = config.get("aws_access_key_id", "")
        self._secret = config.get("aws_secret_access_key", "")
        self._repo = config.get("restic_repository", "")
        self._restic_password = config.get("restic_password", "")
        import boto3

        self._s3 = boto3.client(
            "s3",
            endpoint_url=self._endpoint or None,
            aws_access_key_id=self._key,
            aws_secret_access_key=self._secret,
            region_name=self._region,
        )
        self._started_at = time.time()
        self._timeout_seconds = float(
            config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
        )

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def deadline(self) -> float:
        """Wall-clock time by which the run must finish; past it the worker marks state timed_out."""
        return self._started_at + self._timeout_seconds

    def _restic_env(self) -> dict:
        return {
            **os.environ,
            "RESTIC_REPOSITORY": self._repo,
            "RESTIC_PASSWORD": self._restic_password,
            "AWS_ACCESS_KEY_ID": self._key,
            "AWS_SECRET_ACCESS_KEY": self._secret,
            "AWS_DEFAULT_REGION": self._region,
        }

    def restic_snapshot(self, tag: str) -> None:
        if not (self._repo and self._restic_password):
            print(
                "[eval] no restic repo/password in config -- skipping snapshot",
                tag,
                flush=True,
            )
            return
        env = self._restic_env()
        if (
            subprocess.run(
                ["restic", "cat", "config"], env=env, capture_output=True, text=True
            ).returncode
            != 0
        ):
            init = subprocess.run(
                ["restic", "init"], env=env, capture_output=True, text=True
            )
            if init.returncode != 0:
                print(
                    "[eval] restic init failed: {}".format(
                        (init.stderr or "").strip()[:300]
                    ),
                    flush=True,
                )
        result = subprocess.run(
            ["restic", "backup", HOST_DIR, "--tag", tag, *_RESTIC_EXCLUDES],
            env=env,
            capture_output=True,
            text=True,
        )
        print(
            "[eval] restic snapshot {} rc={} {}".format(
                tag,
                result.returncode,
                (result.stderr or "").strip()[:200] if result.returncode else "",
            ),
            flush=True,
        )

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key="{}/{}".format(self._prefix, key),
            Body=data,
            ContentType=content_type,
        )

    def write_state(self, waits_done: int, num_turns: int, test_state: str) -> None:
        payload = {
            "eval_name": self._config.get("eval_name"),
            "case_name": self._config.get("case_name"),
            "waits_done": waits_done,
            "num_turns": num_turns,
            "test_state": test_state,
            "timed_out": test_state == "timed_out",
            "started_at": _iso(self._started_at),
            "elapsed_seconds": round(time.time() - self._started_at, 1),
            "timeout_seconds": self._timeout_seconds,
        }
        self._put(
            "state.json",
            json.dumps(payload, indent=2).encode("utf-8"),
            "application/json",
        )

    def upload_transcript(self, events: list[dict]) -> None:
        body = "\n".join(json.dumps(event) for event in events).encode("utf-8")
        self._put("artifacts/full_transcript.jsonl", body, "application/x-ndjson")
