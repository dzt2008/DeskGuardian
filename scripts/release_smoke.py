#!/usr/bin/env python3
"""Validate release metadata and smoke-test a frozen DeskGuardian executable."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def _read_version() -> str:
    version_file = ROOT / "version.py"
    tree = ast.parse(
        version_file.read_text(encoding="utf-8"), filename=str(version_file)
    )
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            version = node.value.value.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", version):
                return version
            raise RuntimeError(
                "version.py contains an unsupported VERSION value: %r" % version
            )
    raise RuntimeError("version.py does not define a literal VERSION string")


def validate_release(tag: str) -> None:
    """Require the v-prefixed tag, version.py, and CHANGELOG heading to agree."""
    version = _read_version()
    expected_tag = "v" + version
    if tag != expected_tag:
        raise RuntimeError(
            "release tag %r does not match version.py (%r)" % (tag, expected_tag)
        )

    changelog_path = ROOT / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    heading = re.compile(
        r"^##\s+\[" + re.escape(version) + r"\]\s+-\s+\d{4}-\d{2}-\d{2}\s*$",
        re.MULTILINE,
    )
    if not heading.search(changelog):
        raise RuntimeError(
            "CHANGELOG.md must contain a dated '## [%s] - YYYY-MM-DD' section" % version
        )
    print("release metadata OK: %s" % tag)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def smoke_executable(executable: Path, timeout: float) -> None:
    """Check CLI startup and one real HTTP response without sending notifications."""
    executable = executable.expanduser().resolve()
    if not executable.is_file():
        raise RuntimeError("executable does not exist: %s" % executable)

    help_result = subprocess.run(
        [str(executable), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if help_result.returncode != 0:
        raise RuntimeError(
            "executable --help failed with exit code %d:\n%s"
            % (help_result.returncode, help_result.stderr[-2000:])
        )
    help_text = help_result.stdout + help_result.stderr
    if "DeskGuardian" not in help_text or "web" not in help_text:
        raise RuntimeError("executable --help output is missing expected commands")

    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="deskguardian-release-smoke-") as data_dir:
        # The Web security layer validates Host against the configured port, so
        # keep the isolated config and command-line listener in agreement.
        Path(data_dir, "config.json").write_text(
            json.dumps({"port": port}, ensure_ascii=False), encoding="utf-8"
        )
        environment = os.environ.copy()
        environment["DESKGUARDIAN_DATA_DIR"] = data_dir
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            [
                str(executable),
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-browser",
                "--no-scheduler",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + timeout
        last_error: Optional[BaseException] = None
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout is not None else ""
                    raise RuntimeError(
                        "executable Web process exited early with code %s:\n%s"
                        % (process.returncode, output[-4000:])
                    )
                try:
                    with urlopen("http://127.0.0.1:%d/" % port, timeout=2) as response:
                        body = response.read().decode("utf-8", errors="replace")
                        if response.status != 200:
                            raise RuntimeError(
                                "Web smoke returned HTTP %d" % response.status
                            )
                        if "DeskGuardian" not in body:
                            raise RuntimeError(
                                "Web smoke response is missing DeskGuardian content"
                            )
                        print("executable smoke OK: %s" % executable.name)
                        return
                except (URLError, OSError, RuntimeError) as exc:
                    last_error = exc
                    time.sleep(0.25)
            raise RuntimeError("Web smoke timed out: %s" % last_error)
        finally:
            _stop_process(process)


def _release_architecture() -> str:
    machine = platform.machine().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return aliases.get(machine, re.sub(r"[^a-z0-9_.-]", "-", machine) or "unknown")


def _sha256_file(path: Path) -> Path:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text("%s  %s\n" % (digest.hexdigest(), path.name), encoding="ascii")
    return checksum


def package_release(executable: Path, output_dir: Path) -> None:
    """Create a native release asset and its SHA256 checksum."""
    executable = executable.expanduser().resolve()
    if not executable.is_file():
        raise RuntimeError("executable does not exist: %s" % executable)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    version = _read_version()
    architecture = _release_architecture()
    notices = ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md")
    for notice in notices:
        if not (ROOT / notice).is_file():
            raise RuntimeError("required release notice does not exist: %s" % notice)

    if sys.platform == "win32":
        asset = output_dir / (
            "deskguardian-%s-windows-%s.exe" % (version, architecture)
        )
        shutil.copy2(executable, asset)
        # GitHub Release uploads these as directly readable companion assets.
        for notice in notices:
            shutil.copy2(ROOT / notice, output_dir / notice)
    else:
        operating_system = "macos" if sys.platform == "darwin" else "linux"
        directory_name = "DeskGuardian-%s-%s-%s" % (
            version,
            operating_system,
            architecture,
        )
        with tempfile.TemporaryDirectory(
            prefix="deskguardian-release-package-"
        ) as temporary:
            staging = Path(temporary) / directory_name
            staging.mkdir()
            destination = staging / "deskguardian"
            shutil.copy2(executable, destination)
            destination.chmod(destination.stat().st_mode | 0o111)
            for notice in notices:
                shutil.copy2(ROOT / notice, staging / notice)
            asset = output_dir / (directory_name.lower() + ".tar.gz")
            with tarfile.open(asset, "w:gz") as archive:
                archive.add(staging, arcname=directory_name)

    checksum = _sha256_file(asset)
    print("release asset: %s" % asset)
    print("checksum: %s" % checksum)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate tag and release metadata"
    )
    validate_parser.add_argument("tag", help="Git tag, for example v0.1.0")

    smoke_parser = subparsers.add_parser(
        "smoke", help="smoke-test a PyInstaller executable"
    )
    smoke_parser.add_argument("executable", type=Path)
    smoke_parser.add_argument("--timeout", type=float, default=30.0)

    package_parser = subparsers.add_parser(
        "package", help="package a native executable and write its SHA256"
    )
    package_parser.add_argument("executable", type=Path)
    package_parser.add_argument(
        "--output-dir", type=Path, default=Path("release-assets")
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            validate_release(args.tag)
        elif args.command == "smoke":
            if args.timeout <= 0:
                raise RuntimeError("--timeout must be positive")
            smoke_executable(args.executable, args.timeout)
        else:
            package_release(args.executable, args.output_dir)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("release smoke failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
