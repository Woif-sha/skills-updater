#!/usr/bin/env python3
"""Repeatable local benchmark for the three skills-updater update paths.

The benchmark uses a 160-file, roughly 3 MiB payload, matching the upper end of
the Skill payloads installed on the machine where the benchmark was introduced.
Remote HEAD probes and snapshot archive downloads are deterministic fakes; Git
uses a local bare repository. Network timings are therefore intentionally not
reported, while request/fetch counts and all local work are measured.
"""

from __future__ import annotations

import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Callable, Iterator
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.agent_skill_updater as updater  # noqa: E402
import scripts.skills_registry as registry  # noqa: E402
import scripts.update_agent_skills as cli  # noqa: E402


FILE_COUNT = 160
FILE_SIZE = 20_000
PAYLOAD_BYTES = FILE_COUNT * FILE_SIZE
BASE_SHA = "a" * 40
REMOTE_SHA = "b" * 40


@dataclass
class Measurement:
    calls: int = 0
    seconds: float = 0.0


class Recorder:
    def __init__(self) -> None:
        self.measurements: dict[str, Measurement] = defaultdict(Measurement)

    def call(self, name: str, operation: Callable, *args, **kwargs):
        started = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            measurement = self.measurements[name]
            measurement.calls += 1
            measurement.seconds += time.perf_counter() - started

    def wrap(self, name: str, operation: Callable) -> Callable:
        def measured(*args, **kwargs):
            return self.call(name, operation, *args, **kwargs)

        return measured

    def count(self, name: str) -> None:
        self.measurements[name].calls += 1

    def as_dict(self) -> dict[str, dict[str, float | int]]:
        return {
            name: {
                "calls": value.calls,
                "milliseconds": round(value.seconds * 1_000, 3),
            }
            for name, value in sorted(self.measurements.items())
        }


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None


def payload_entries(version: str) -> Iterator[tuple[str, bytes]]:
    yield "SKILL.md", f"---\nname: benchmark\n---\nversion: {version}\n".encode()
    filler = (version.encode() * (FILE_SIZE // len(version) + 1))[:FILE_SIZE]
    for index in range(FILE_COUNT - 1):
        yield f"rules/group-{index % 8:02d}/rule-{index:04d}.md", filler


def write_payload(root: Path, version: str) -> None:
    for relative, content in payload_entries(version):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def make_archive(commit: str, version: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, content in payload_entries(version):
            archive.writestr(f"benchmark-{commit}/{relative}", content)
    return stream.getvalue()


def snapshot_metadata(installed_base: str) -> dict[str, str]:
    return {
        "source": "example/benchmark",
        "sourceType": "git",
        "repoUrl": "https://github.com/example/benchmark",
        "subpath": ".",
        "installedBaseVersion": installed_base,
    }


def run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def configure_git(repo: Path) -> None:
    run_git(repo, "config", "user.name", "Skills Updater Benchmark")
    run_git(repo, "config", "user.email", "benchmark@example.invalid")
    run_git(repo, "config", "core.autocrlf", "false")


def create_snapshot_fixture(root: Path, installed_base: str = BASE_SHA) -> Path:
    skills_root = root / "skills"
    local = skills_root / "benchmark"
    write_payload(local, "base")
    (local / ".openskills.json").write_text(
        json.dumps(snapshot_metadata(installed_base)),
        encoding="utf-8",
    )
    return skills_root


def create_git_fixture(root: Path) -> Path:
    remote = root / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    seed = root / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    configure_git(seed)
    write_payload(seed, "base")
    run_git(seed, "add", ".")
    run_git(seed, "commit", "-m", "base")
    run_git(seed, "push", "-u", "origin", "main")
    base_sha = run_git(seed, "rev-parse", "HEAD")

    skills_root = root / "skills"
    local = skills_root / "benchmark"
    skills_root.mkdir()
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    configure_git(local)
    (local / ".openskills.json").write_text(
        json.dumps(
            {
                "source": "local/benchmark",
                "sourceType": "git",
                "repoUrl": run_git(local, "config", "--get", "remote.origin.url"),
                "subpath": ".",
                "installedBaseVersion": base_sha,
            }
        ),
        encoding="utf-8",
    )

    changed = seed / "rules" / "group-00" / "rule-0000.md"
    changed.write_bytes(b"remote" * (FILE_SIZE // 6))
    run_git(seed, "add", ".")
    run_git(seed, "commit", "-m", "remote update")
    run_git(seed, "push", "origin", "main")
    return skills_root


def instrument(recorder: Recorder) -> ExitStack:
    stack = ExitStack()
    for name, metric in (
        ("iter_skill_payload_files", "payload_file_scan"),
        ("iter_skill_payload_directories", "payload_directory_scan"),
        ("directory_signature", "payload_signature"),
        ("_copy_directory_contents", "payload_copy"),
        ("_write_bytes_atomic", "transaction_write"),
    ):
        original = getattr(updater, name)
        stack.enter_context(mock.patch.object(updater, name, recorder.wrap(metric, original)))

    original_extractall = zipfile.ZipFile.extractall
    stack.enter_context(
        mock.patch.object(
            zipfile.ZipFile,
            "extractall",
            recorder.wrap("archive_extract", original_extractall),
        )
    )
    original_registry_write = registry._write_json_atomic
    stack.enter_context(
        mock.patch.object(
            registry,
            "_write_json_atomic",
            recorder.wrap("registry_write", original_registry_write),
        )
    )
    original_subprocess_run = subprocess.run
    stack.enter_context(
        mock.patch.object(
            subprocess,
            "run",
            recorder.wrap("subprocess", original_subprocess_run),
        )
    )
    original_git_fetch = updater._git_fetch_remote_branch
    stack.enter_context(
        mock.patch.object(
            updater,
            "_git_fetch_remote_branch",
            recorder.wrap("network_git_fetch", original_git_fetch),
        )
    )
    return stack


def invoke_cli(skills_root: Path, recorder: Recorder) -> tuple[int, float]:
    original_sync = registry.sync_registry

    def sync_fixture() -> dict:
        return recorder.call("registry_sync", original_sync, skills_root)

    started = time.perf_counter()
    with mock.patch.object(cli, "sync_registry", side_effect=sync_fixture):
        with mock.patch.object(sys, "argv", ["update_agent_skills.py", "--skill", "benchmark", "--json"]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    cli.main()
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)
                else:
                    exit_code = 0
    return exit_code, time.perf_counter() - started


def snapshot_update_once() -> dict:
    with tempfile.TemporaryDirectory(prefix="skills-updater-bench-snapshot-") as temp_dir:
        root = Path(temp_dir)
        skills_root = create_snapshot_fixture(root)
        archives = {
            BASE_SHA: make_archive(BASE_SHA, "base"),
            REMOTE_SHA: make_archive(REMOTE_SHA, "remote"),
        }
        recorder = Recorder()

        def fake_probe(_repo_url: str) -> str:
            recorder.count("network_head_probe")
            return REMOTE_SHA

        def fake_urlopen(request, timeout=None):
            del timeout
            recorder.count("network_archive_download")
            commit = str(request.full_url).rsplit("/", 1)[-1]
            return FakeResponse(archives[commit])

        with instrument(recorder):
            with mock.patch.object(updater, "_fetch_remote_commit_sha", side_effect=fake_probe):
                with mock.patch.object(updater.urllib.request, "urlopen", side_effect=fake_urlopen):
                    exit_code, duration = invoke_cli(skills_root, recorder)
        if exit_code != 0:
            raise RuntimeError(f"snapshot benchmark failed with exit code {exit_code}")
        return {"wall_milliseconds": duration * 1_000, "metrics": recorder.as_dict()}


def metadata_only_once() -> dict:
    with tempfile.TemporaryDirectory(prefix="skills-updater-bench-metadata-") as temp_dir:
        root = Path(temp_dir)
        skills_root = create_snapshot_fixture(root, REMOTE_SHA[:12])
        recorder = Recorder()

        def fake_probe(_repo_url: str) -> str:
            recorder.count("network_head_probe")
            return REMOTE_SHA

        with instrument(recorder):
            with mock.patch.object(updater, "_fetch_remote_commit_sha", side_effect=fake_probe):
                exit_code, duration = invoke_cli(skills_root, recorder)
        if exit_code != 0:
            raise RuntimeError(f"metadata-only benchmark failed with exit code {exit_code}")
        return {"wall_milliseconds": duration * 1_000, "metrics": recorder.as_dict()}


def git_worktree_once() -> dict:
    with tempfile.TemporaryDirectory(prefix="skills-updater-bench-git-") as temp_dir:
        root = Path(temp_dir)
        skills_root = create_git_fixture(root)
        recorder = Recorder()
        with instrument(recorder):
            exit_code, duration = invoke_cli(skills_root, recorder)
        if exit_code != 0:
            raise RuntimeError(f"Git worktree benchmark failed with exit code {exit_code}")
        return {"wall_milliseconds": duration * 1_000, "metrics": recorder.as_dict()}


def aggregate(runs: list[dict]) -> dict:
    metric_names = sorted({name for run in runs for name in run["metrics"]})
    metrics = {}
    for name in metric_names:
        calls = [run["metrics"].get(name, {}).get("calls", 0) for run in runs]
        milliseconds = [
            run["metrics"].get(name, {}).get("milliseconds", 0.0) for run in runs
        ]
        metrics[name] = {
            "median_calls": median(calls),
            "median_milliseconds": round(median(milliseconds), 3),
        }
    wall_times = [run["wall_milliseconds"] for run in runs]
    return {
        "median_wall_milliseconds": round(median(wall_times), 3),
        "min_wall_milliseconds": round(min(wall_times), 3),
        "max_wall_milliseconds": round(max(wall_times), 3),
        "metrics": metrics,
    }


def main() -> None:
    repetitions = 5
    scenarios = {
        "snapshot_update": snapshot_update_once,
        "git_worktree_update": git_worktree_once,
        "metadata_only": metadata_only_once,
    }
    results = {
        name: aggregate([scenario() for _ in range(repetitions)])
        for name, scenario in scenarios.items()
    }
    print(
        json.dumps(
            {
                "environment": {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "filesystem": os.name,
                },
                "fixture": {
                    "files": FILE_COUNT,
                    "payload_bytes": PAYLOAD_BYTES,
                    "repetitions": repetitions,
                    "network_timing": "excluded; deterministic local fakes count requests",
                },
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
