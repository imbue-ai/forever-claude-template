Stopped the `outer_trigger` (vps-docker / gVisor) backup path from retaining local btrfs snapshots between ticks, so a workspace's disk is no longer held hostage by already-deleted data:

- `max_local_snapshots` now defaults to 0 (was 5). After restic reads a tick's snapshot, every local snapshot is deleted -- exactly like the `btrfs_local` (lima) path already did. Previously up to 5 read-only snapshots persisted, and because btrfs is copy-on-write, each one pinned the blocks of every file deleted since it was taken, so freeing disk space did not reclaim it until the snapshots rotated out (up to ~5 backup intervals later).

- Nothing consumes retained local snapshots: restore reads from the remote restic repository, not from these. The "keep the newest N" count was an arbitrary buffer from the snapshot-rotation fix, not a functional requirement. Unique per-tick snapshot names (the actual fix for the gVisor stale-gofer-handle bug) are unchanged, so deleting all snapshots after each backup never reintroduces a reused path.

- The `max_local_snapshots` field is retained (not removed) so any lingering boot-time constructor that still sets it keeps working; it just defaults to 0.
