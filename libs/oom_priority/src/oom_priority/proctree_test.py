"""Tests for the /proc process-tree walk, against a fake ``/proc`` layout."""

from pathlib import Path

from oom_priority.proctree import list_descendant_pids


def _write_fake_proc_children(proc_dir: Path, pid: int, children: list[int]) -> None:
    task_dir = proc_dir / str(pid) / "task" / str(pid)
    task_dir.mkdir(parents=True)
    (task_dir / "children").write_text(" ".join(str(child) for child in children) + " ")


def test_descendant_walk_is_recursive_and_survives_gaps(tmp_path: Path) -> None:
    # 10 -> 11 -> 12, plus 10 -> 13 where 13 has no /proc entry (exited).
    _write_fake_proc_children(tmp_path, 10, [11, 13])
    _write_fake_proc_children(tmp_path, 11, [12])
    _write_fake_proc_children(tmp_path, 12, [])
    found = list_descendant_pids(10, proc_dir=tmp_path)
    assert sorted(found) == [11, 12, 13]


def test_descendant_walk_on_a_host_without_proc(tmp_path: Path) -> None:
    assert list_descendant_pids(10, proc_dir=tmp_path / "none") == []
