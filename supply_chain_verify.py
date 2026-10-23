#!/usr/bin/env python3
"""Build and verify OntoGenix packages with the declared setuptools backend."""

import argparse
import ast
import base64
import csv
import hashlib
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


PIN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+$")
HASH_PATTERN = re.compile(r"--hash=sha256:[0-9a-f]{64}$")
MANIFEST_TARGET_FILES = ("requirements.txt", "build-requirements.txt", "pyproject.toml")
CORE_SOURCE_FILES = (
    "headless.py",
    "GUI/__main__.py",
    "pyproject.toml",
    "requirements.txt",
    "LICENSE",
    "CITATION.cff",
)


def verify_pinned_requirements(req_path):
    """Return pinned requirements with SHA256 hashes, rejecting unsafe entries."""
    req_path = Path(req_path)
    if not req_path.is_file():
        raise ValueError(f"Requirements file does not exist: {req_path}")
    requirements = []
    normalized_names = set()

    logical_lines = []
    parts = []
    start_line = None
    for line_number, raw_line in enumerate(req_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not parts and (not line or line.startswith("#")):
            continue
        if start_line is None:
            start_line = line_number
        continued = line.endswith("\\")
        parts.append(line[:-1].rstrip() if continued else line)
        if not continued:
            logical_lines.append((start_line, " ".join(parts).strip()))
            parts = []
            start_line = None

    if parts:
        raise ValueError(f"Unterminated requirement continuation on line {start_line} of {req_path}")

    for line_number, line in logical_lines:
        if re.search(r"(?i)(?:--trusted-host|--extra-index-url|--index-url|-i\s+https?://|--find-links)", line):
            raise ValueError(f"Insecure pip option on line {line_number} of {req_path}")
        tokens = line.split()
        if not tokens or not PIN_PATTERN.fullmatch(tokens[0]):
            raise ValueError(f"Requirement on line {line_number} is not exactly pinned: {line!r}")
        hashes = tokens[1:]
        if not hashes or any(not HASH_PATTERN.fullmatch(value) for value in hashes):
            raise ValueError(f"Requirement on line {line_number} must have valid SHA256 hashes: {line!r}")

        requirement = tokens[0]
        name, _version = requirement.split("==", 1)
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in normalized_names:
            raise ValueError(f"Duplicate requirement name on line {line_number}: {name}")
        normalized_names.add(normalized_name)
        requirements.append(requirement)

    return requirements


def _project_dependencies(project_path):
    project_path = Path(project_path)
    if not project_path.is_file():
        raise ValueError(f"Project metadata does not exist: {project_path}")
    contents = project_path.read_text(encoding="utf-8")
    section_match = re.search(r"(?ms)^\[project\][ \t]*\n(.*?)(?=^\[|\Z)", contents)
    if not section_match:
        raise ValueError(f"Project metadata has no [project] section: {project_path}")
    dependencies_match = re.search(r"(?ms)^[ \t]*dependencies[ \t]*=[ \t]*(\[.*?\])", section_match.group(1))
    if not dependencies_match:
        raise ValueError(f"Project metadata has no dependencies list: {project_path}")
    try:
        dependencies = ast.literal_eval(dependencies_match.group(1))
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"Invalid project dependencies list in {project_path}") from error
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        raise ValueError(f"Project dependencies must be a list of strings: {project_path}")
    return dependencies


def _verify_dependency_contract(repo_root):
    repo_root = Path(repo_root)
    requirements = verify_pinned_requirements(repo_root / "requirements.txt")
    verify_pinned_requirements(repo_root / "build-requirements.txt")
    if _project_dependencies(repo_root / "pyproject.toml") != requirements:
        raise ValueError("pyproject.toml project dependencies do not match requirements.txt")


def _digest(path, algorithm):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    hasher = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_checksum(path, filename, digest):
    Path(path).write_text(f"{digest}  {filename}\n", encoding="ascii")


def _parse_checksum_lines(path, algorithm):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Checksum manifest does not exist: {path}")
    expected_length = hashlib.new(algorithm).digest_size * 2
    checksums = {}
    for line_number, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-fA-F]+)\s+([^\s]+)", line)
        if not match or len(match.group(1)) != expected_length:
            raise ValueError(f"Malformed {algorithm.upper()} checksum on line {line_number} of {path}")
        digest, filename = match.groups()
        if filename in checksums:
            raise ValueError(f"Duplicate checksum entry for {filename} in {path}")
        checksums[filename] = digest.lower()
    if not checksums:
        raise ValueError(f"No checksum entries found in {path}")
    return checksums


def _verify_single_checksum(checksum_path, filename, algorithm, actual_digest):
    checksums = _parse_checksum_lines(checksum_path, algorithm)
    if set(checksums) != {filename}:
        raise ValueError(f"{checksum_path} must contain exactly one entry for {filename}")
    if checksums[filename] != actual_digest:
        raise ValueError(f"Checksum mismatch for {filename} in {checksum_path}")


def write_manifest_checksums(repo_root):
    """Write SHA256 and MD5 manifests for the pinned requirements and project metadata."""
    repo_root = Path(repo_root)
    for algorithm, manifest_name in (("sha256", "requirements.sha256"), ("md5", "requirements.md5")):
        entries = []
        for filename in MANIFEST_TARGET_FILES:
            entries.append(f"{_digest(repo_root / filename, algorithm)}  {filename}")
        (repo_root / manifest_name).write_text("\n".join(entries) + "\n", encoding="ascii")


def verify_manifest_checksums(repo_root):
    """Verify pinned requirements and their SHA256/MD5 manifest entries."""
    repo_root = Path(repo_root)
    _verify_dependency_contract(repo_root)
    expected_files = MANIFEST_TARGET_FILES
    for algorithm, manifest_name in (("sha256", "requirements.sha256"), ("md5", "requirements.md5")):
        manifest_path = repo_root / manifest_name
        checksums = _parse_checksum_lines(manifest_path, algorithm)
        if set(checksums) != set(expected_files):
            missing = sorted(set(expected_files) - set(checksums))
            raise ValueError(f"{manifest_path} is missing checksum entries: {', '.join(missing)}")
        for filename in expected_files:
            if checksums[filename] != _digest(repo_root / filename, algorithm):
                raise ValueError(f"Checksum mismatch for {filename} in {manifest_path}")


def _package_files(repo_root):
    """Collect runtime sources and metadata without caches or local data."""
    repo_root = Path(repo_root)
    filenames = [
        "CITATION.cff",
        "LICENSE",
        "README.md",
        "headless.py",
        "pyproject.toml",
        "requirements.txt",
        "supply_chain_verify.py",
    ]
    package_files = {}
    for filename in filenames:
        path = repo_root / filename
        if not path.is_file():
            raise ValueError(f"Required package source is missing: {filename}")
        package_files[filename] = path.read_bytes()

    gui_root = repo_root / "GUI"
    if not gui_root.is_dir():
        raise ValueError("Required package source directory is missing: GUI")
    for path in sorted(gui_root.rglob("*")):
        relative = path.relative_to(repo_root)
        if (
            not path.is_file()
            or "__pycache__" in relative.parts
            or path.name == ".env"
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        package_files[relative.as_posix()] = path.read_bytes()

    return package_files


def _record_hash(contents):
    encoded = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _core_source_hashes(repo_root):
    hashes = {}
    for filename in CORE_SOURCE_FILES:
        path = Path(repo_root) / filename
        if not path.is_file():
            raise ValueError(f"Required core source file is missing: {filename}")
        hashes[filename] = _digest(path, "sha256")
    return hashes


def _verify_wheel_record(wheel_path):
    with zipfile.ZipFile(wheel_path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Wheel contains duplicate archive entries: {wheel_path}")
        for name in names:
            archive_path = PurePosixPath(name)
            if archive_path.is_absolute() or ".." in archive_path.parts or "\\" in name:
                raise ValueError(f"Unsafe path in wheel archive: {name}")

        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise ValueError(f"Wheel must contain exactly one RECORD file: {wheel_path}")
        record_name = record_names[0]
        rows = csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"), newline=""))
        record_entries = {}
        for row in rows:
            if len(row) != 3 or row[0] in record_entries:
                raise ValueError(f"Malformed or duplicate RECORD entry in {wheel_path}")
            record_entries[row[0]] = (row[1], row[2])
        if set(record_entries) != set(names):
            raise ValueError(f"Wheel RECORD does not list every archive entry: {wheel_path}")

        for name in names:
            recorded_hash, recorded_size = record_entries[name]
            if name == record_name:
                if recorded_hash or recorded_size:
                    raise ValueError(f"Wheel RECORD must leave its own hash and size empty: {wheel_path}")
                continue
            contents = archive.read(name)
            if recorded_hash != _record_hash(contents) or recorded_size != str(len(contents)):
                raise ValueError(f"Wheel RECORD verification failed for {name} in {wheel_path}")


def _verify_artifact_sidecar(dist_dir, filename, algorithm, actual_digest):
    sidecar_path = Path(dist_dir) / f"{filename}.{algorithm}"
    _verify_single_checksum(sidecar_path, filename, algorithm, actual_digest)


def verify_dist_checksums(dist_dir):
    """Verify distribution manifests, artifact sidecars, and wheel RECORD hashes."""
    dist_dir = Path(dist_dir)
    sha_path = dist_dir / "CHECKSUMS.sha256"
    md5_path = dist_dir / "CHECKSUMS.md5"
    sha_checksums = _parse_checksum_lines(sha_path, "sha256")
    md5_checksums = _parse_checksum_lines(md5_path, "md5")
    if set(sha_checksums) != set(md5_checksums):
        raise ValueError("CHECKSUMS.sha256 and CHECKSUMS.md5 list different artifacts")

    filenames = set(sha_checksums)
    if not any(filename.endswith(".whl") for filename in filenames):
        raise ValueError("CHECKSUMS manifests must list a wheel package")
    if not any(filename.endswith(".tar.gz") for filename in filenames):
        raise ValueError("CHECKSUMS manifests must list a source archive")
    if any(Path(filename).name != filename for filename in filenames):
        raise ValueError("Distribution checksum entries must use artifact filenames only")

    discovered = {
        path.name
        for path in dist_dir.iterdir()
        if path.is_file() and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    }
    if discovered != filenames:
        raise ValueError("Distribution manifests do not list exactly the packages in the directory")

    for filename in sorted(filenames):
        artifact = dist_dir / filename
        if not artifact.is_file():
            raise ValueError(f"Distribution package is missing: {filename}")
        sha256_digest = _digest(artifact, "sha256")
        md5_digest = _digest(artifact, "md5")
        if sha_checksums[filename] != sha256_digest:
            raise ValueError(f"SHA256 checksum mismatch for {filename}")
        if md5_checksums[filename] != md5_digest:
            raise ValueError(f"MD5 checksum mismatch for {filename}")
        _verify_artifact_sidecar(dist_dir, filename, "sha256", sha256_digest)
        _verify_artifact_sidecar(dist_dir, filename, "md5", md5_digest)
        if filename.endswith(".whl"):
            _verify_wheel_record(artifact)


def write_dist_checksums(dist_dir):
    """Write distribution checksums after validating every wheel RECORD."""
    dist_dir = Path(dist_dir)
    artifacts = sorted(
        path
        for path in dist_dir.iterdir()
        if path.is_file() and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    )
    if not any(path.name.endswith(".whl") for path in artifacts):
        raise ValueError("Distribution directory must contain a wheel package")
    if not any(path.name.endswith(".tar.gz") for path in artifacts):
        raise ValueError("Distribution directory must contain a source archive")
    for artifact in artifacts:
        if artifact.name.endswith(".whl"):
            _verify_wheel_record(artifact)

    for algorithm in ("sha256", "md5"):
        entries = []
        for artifact in artifacts:
            digest = _digest(artifact, algorithm)
            _write_checksum(dist_dir / f"{artifact.name}.{algorithm}", artifact.name, digest)
            entries.append(f"{digest}  {artifact.name}")
        (dist_dir / f"CHECKSUMS.{algorithm}").write_text("\n".join(entries) + "\n", encoding="ascii")

    verify_dist_checksums(dist_dir)
    return artifacts


def build_and_verify_packages(dist_dir, repo_root):
    """Build sdist and wheel with setuptools, then verify all checksums."""
    repo_root = Path(repo_root).resolve()
    dist_dir = Path(dist_dir).resolve()
    verify_manifest_checksums(repo_root)
    before_hashes = _core_source_hashes(repo_root)
    package_files = _package_files(repo_root)

    dist_dir.mkdir(parents=True, exist_ok=True)
    from setuptools import build_meta

    with tempfile.TemporaryDirectory() as build_directory:
        build_root = Path(build_directory)
        for filename, contents in package_files.items():
            staged_file = build_root / PurePosixPath(filename)
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            staged_file.write_bytes(contents)

        original_directory = Path.cwd()
        try:
            os.chdir(build_root)
            sdist_filename = build_meta.build_sdist(str(dist_dir))
            wheel_filename = build_meta.build_wheel(str(dist_dir))
        finally:
            os.chdir(original_directory)

    after_hashes = _core_source_hashes(repo_root)
    if after_hashes != before_hashes:
        raise ValueError("Core source files changed during package creation")

    write_dist_checksums(dist_dir)
    return [dist_dir / filename for filename in sorted((sdist_filename, wheel_filename))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--verify-pins", action="store_true", help="verify all requirements are exactly pinned")
    actions.add_argument("--update-manifests", action="store_true", help="write requirements SHA256 and MD5 manifests")
    actions.add_argument("--verify-manifests", action="store_true", help="verify dependency and project metadata manifests")
    actions.add_argument("--build-and-verify", metavar="DIST_DIR", help="build and verify sdist and wheel packages")
    actions.add_argument("--write-dist-checksums", metavar="DIST_DIR", help="write and verify package checksums")
    actions.add_argument("--verify-dist", metavar="DIST_DIR", help="verify package checksums and wheel RECORD")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parent

    try:
        if args.verify_pins:
            _verify_dependency_contract(repo_root)
        elif args.update_manifests:
            write_manifest_checksums(repo_root)
        elif args.verify_manifests:
            verify_manifest_checksums(repo_root)
        elif args.build_and_verify:
            build_and_verify_packages(args.build_and_verify, repo_root)
        elif args.write_dist_checksums:
            write_dist_checksums(args.write_dist_checksums)
        else:
            verify_dist_checksums(args.verify_dist)
    except Exception as error:
        print(f"supply_chain_verify: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
