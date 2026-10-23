"""Standard-library contract checks for dependency and package integrity."""

import base64
import csv
import hashlib
import importlib.util
import io
import re
import shutil
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import supply_chain_verify


REPO_ROOT = Path(__file__).resolve().parent
HAS_SETUPTOOLS = importlib.util.find_spec("setuptools") is not None
TEST_HASH = "a" * 64


def _write_synthetic_distribution(dist_dir):
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = dist_dir / "synthetic-1.0-py3-none-any.whl"
    wheel_files = {
        "synthetic/__init__.py": b"value = 1\n",
        "synthetic-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: synthetic\nVersion: 1.0\n",
    }
    record_name = "synthetic-1.0.dist-info/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for filename, contents in wheel_files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((filename, f"sha256={digest}", str(len(contents))))
    writer.writerow((record_name, "", ""))
    wheel_files[record_name] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in wheel_files.items():
            archive.writestr(filename, contents)

    source_path = dist_dir / "synthetic-1.0.tar.gz"
    source_contents = b"synthetic source package\n"
    with tarfile.open(source_path, "w:gz") as archive:
        info = tarfile.TarInfo("synthetic-1.0/README.txt")
        info.size = len(source_contents)
        archive.addfile(info, io.BytesIO(source_contents))
    return wheel_path, source_path


def _rewrite_wheel_record(wheel_path):
    with zipfile.ZipFile(wheel_path, "r") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
    rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"), newline="")))
    rows[0][1] = "sha256=AAAAAAAA"
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files[record_name] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents)


def _refresh_distribution_checksums(dist_dir):
    artifacts = sorted(path for path in dist_dir.iterdir() if path.name.endswith((".whl", ".tar.gz")))
    for algorithm in ("sha256", "md5"):
        entries = []
        for artifact in artifacts:
            digest = hashlib.new(algorithm, artifact.read_bytes()).hexdigest()
            line = f"{digest}  {artifact.name}"
            (dist_dir / f"{artifact.name}.{algorithm}").write_text(line + "\n", encoding="ascii")
            entries.append(line)
        (dist_dir / f"CHECKSUMS.{algorithm}").write_text("\n".join(entries) + "\n", encoding="ascii")


class DependencyPinningContractTests(unittest.TestCase):
    def test_repository_requirements_are_24_unique_exact_pins(self):
        requirements = supply_chain_verify.verify_pinned_requirements(REPO_ROOT / "requirements.txt")

        self.assertEqual(24, len(requirements))
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+", item) for item in requirements))
        normalized_names = [re.sub(r"[-_.]+", "-", item.split("==", 1)[0]).lower() for item in requirements]
        self.assertEqual(len(normalized_names), len(set(normalized_names)))

    def test_runtime_and_build_requirements_have_hashes(self):
        for filename in ("requirements.txt", "build-requirements.txt"):
            with self.subTest(file=filename):
                requirements = supply_chain_verify.verify_pinned_requirements(REPO_ROOT / filename)
                self.assertTrue(requirements)
                self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+", item) for item in requirements))

    def test_verifier_accepts_exact_pins_and_rejects_unsafe_requirements(self):
        valid_requirements = (
            f"example-package==1.2.3 \\\n"
            f"    --hash=sha256:{TEST_HASH} \\\n"
            f"    --hash=sha256:{'b' * 64}\n"
            f"other_package==4.5 --hash=sha256:{TEST_HASH}\n"
        )
        cases = (
            ("valid continuations", valid_requirements, True),
            ("unpinned", f"example-package --hash=sha256:{TEST_HASH}\n", False),
            ("range operator", f"example-package~=1.2 --hash=sha256:{TEST_HASH}\n", False),
            ("missing hash", "example-package==1.2.3\n", False),
            ("invalid hash", "example-package==1.2.3 --hash=sha256:bad\n", False),
            ("duplicate normalized name", f"example-package==1.0 --hash=sha256:{TEST_HASH}\nexample_package==2.0 --hash=sha256:{TEST_HASH}\n", False),
            ("trusted host", "--trusted-host packages.example.test\n", False),
            ("insecure index URL", "--index-url http://packages.example.test/simple\n", False),
        )

        for name, contents, valid in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary_directory:
                requirements_path = Path(temporary_directory) / "requirements.txt"
                requirements_path.write_text(contents, encoding="utf-8")
                if valid:
                    self.assertEqual(["example-package==1.2.3", "other_package==4.5"],
                                     supply_chain_verify.verify_pinned_requirements(requirements_path))
                else:
                    with self.assertRaises(ValueError):
                        supply_chain_verify.verify_pinned_requirements(requirements_path)


class ManifestChecksumContractTests(unittest.TestCase):
    def test_repository_manifests_verify_without_static_root_sidecars(self):
        self.assertFalse((REPO_ROOT / "requirements.md5").exists())
        self.assertFalse((REPO_ROOT / "requirements.sha256").exists())
        supply_chain_verify.verify_manifest_checksums(REPO_ROOT)

    def test_manifest_verification_rejects_modified_requirements_and_manifests(self):
        for filename in ("requirements.txt", "build-requirements.txt", "requirements.md5", "requirements.sha256"):
            with self.subTest(file=filename), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                for source_name in ("requirements.txt", "build-requirements.txt", "pyproject.toml"):
                    shutil.copyfile(REPO_ROOT / source_name, root / source_name)
                manifest_dir = root / "dist"
                supply_chain_verify.write_manifest_checksums(root, manifest_dir)
                supply_chain_verify.verify_manifest_checksums(root, manifest_dir)

                target = manifest_dir / filename if filename in ("requirements.md5", "requirements.sha256") else root / filename
                if filename == "requirements.txt":
                    contents = target.read_text(encoding="utf-8")
                    mutated = re.sub(
                        r"--hash=sha256:[0-9a-f]{64}",
                        "--hash=sha256:" + ("f" * 64),
                        contents,
                        count=1,
                    )
                    self.assertNotEqual(contents, mutated)
                    target.write_text(mutated, encoding="utf-8")
                elif filename == "build-requirements.txt":
                    contents = target.read_text(encoding="utf-8")
                    mutated = re.sub(
                        r"--hash=sha256:[0-9a-f]{64}",
                        "--hash=sha256:" + ("f" * 64),
                        contents,
                        count=1,
                    )
                    self.assertNotEqual(contents, mutated)
                    target.write_text(mutated, encoding="utf-8")
                else:
                    digest, separator, remainder = target.read_text(encoding="ascii").partition("  ")
                    changed_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
                    target.write_text(changed_digest + separator + remainder, encoding="ascii")

                with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                    supply_chain_verify.verify_manifest_checksums(root, manifest_dir)

    def test_dependabot_version_bump_passes_without_manual_manifest_refresh(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for source_name in ("requirements.txt", "build-requirements.txt", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / source_name, root / source_name)
            self.assertFalse((root / "requirements.md5").exists())
            self.assertFalse((root / "requirements.sha256").exists())

            requirements_path = root / "requirements.txt"
            first_pin = supply_chain_verify.verify_pinned_requirements(requirements_path)[0]
            bumped_pin = f"{first_pin}.1"
            requirements_text = requirements_path.read_text(encoding="utf-8")
            self.assertIn(first_pin, requirements_text)
            requirements_text = requirements_text.replace(first_pin, bumped_pin, 1)
            requirements_text = re.sub(
                r"--hash=sha256:[0-9a-f]{64}",
                "--hash=sha256:" + ("a" * 64),
                requirements_text,
                count=1,
            )
            self.assertIn(f"--hash=sha256:{TEST_HASH}", requirements_text)
            requirements_path.write_text(requirements_text, encoding="utf-8")

            with self.assertRaises(ValueError) as error:
                supply_chain_verify.verify_manifest_checksums(root)
            self.assertEqual(
                "pyproject.toml project dependencies do not match requirements.txt",
                str(error.exception),
            )

            project_path = root / "pyproject.toml"
            pyproject_text = project_path.read_text(encoding="utf-8")
            self.assertIn(f'"{first_pin}"', pyproject_text)
            pyproject_text = pyproject_text.replace(f'"{first_pin}"', f'"{bumped_pin}"', 1)
            project_path.write_text(pyproject_text, encoding="utf-8")
            supply_chain_verify.verify_manifest_checksums(root)
            self.assertFalse((root / "requirements.md5").exists())
            self.assertFalse((root / "requirements.sha256").exists())

            aligned_project = project_path.read_text(encoding="utf-8")
            project_path.write_text(aligned_project.replace(f'"{bumped_pin}",\n', "", 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                supply_chain_verify.verify_manifest_checksums(root)

            project_path.write_text(aligned_project.replace("]\n", '    "unexpected-package==1.0",\n]\n', 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                supply_chain_verify.verify_manifest_checksums(root)

    def test_contract_tests_handle_bumped_dependency_versions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_repo = Path(temporary_directory)
            setuptools_pin = next(
                pin for pin in supply_chain_verify.verify_pinned_requirements(REPO_ROOT / "build-requirements.txt")
                if pin.startswith("setuptools==")
            )
            beautifulsoup_pin = next(
                pin for pin in supply_chain_verify.verify_pinned_requirements(REPO_ROOT / "requirements.txt")
                if pin.startswith("beautifulsoup4==")
            )
            for source_name in ("requirements.txt", "build-requirements.txt", "pyproject.toml"):
                contents = (REPO_ROOT / source_name).read_text(encoding="utf-8")
                contents = contents.replace(setuptools_pin, "setuptools==84.0.0")
                contents = contents.replace(beautifulsoup_pin, "beautifulsoup4==4.13.0")
                (temp_repo / source_name).write_text(contents, encoding="utf-8")

            with unittest.mock.patch.object(sys.modules[__name__], "REPO_ROOT", temp_repo):
                self.test_manifest_verification_rejects_modified_requirements_and_manifests()
                self.test_dependabot_version_bump_passes_without_manual_manifest_refresh()


class DistributionChecksumContractTests(unittest.TestCase):
    def test_write_and_verify_manifest_checksums_in_dist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            dist_dir = Path(temporary_directory) / "dist"
            supply_chain_verify.write_manifest_checksums(REPO_ROOT, dist_dir)

            self.assertTrue((dist_dir / "requirements.md5").is_file())
            self.assertTrue((dist_dir / "requirements.sha256").is_file())
            supply_chain_verify.verify_manifest_checksums(REPO_ROOT, dist_dir)

    @unittest.skipUnless(HAS_SETUPTOOLS, "setuptools is not installed in host Python")
    def test_build_creates_packages_and_all_trust_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            dist_dir = Path(temporary_directory) / "dist"
            artifacts = supply_chain_verify.build_and_verify_packages(dist_dir, REPO_ROOT)

            self.assertEqual(2, len(artifacts))
            self.assertTrue(any(path.name.endswith(".tar.gz") for path in artifacts))
            self.assertTrue(any(path.name.endswith(".whl") for path in artifacts))
            for artifact in artifacts:
                self.assertTrue(artifact.is_file())
                for algorithm in ("md5", "sha256"):
                    self.assertTrue((dist_dir / f"{artifact.name}.{algorithm}").is_file())
            for manifest in ("CHECKSUMS.md5", "CHECKSUMS.sha256"):
                self.assertTrue((dist_dir / manifest).is_file())
            self.assertTrue((dist_dir / "requirements.md5").is_file())
            self.assertTrue((dist_dir / "requirements.sha256").is_file())

            supply_chain_verify.verify_dist_checksums(dist_dir)
            supply_chain_verify.verify_manifest_checksums(REPO_ROOT, dist_dir)

    def test_checksum_writer_handles_synthetic_archives_without_setuptools(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            dist_dir = Path(temporary_directory) / "dist"
            artifacts = _write_synthetic_distribution(dist_dir)
            written_artifacts = supply_chain_verify.write_dist_checksums(dist_dir)

            self.assertEqual(list(artifacts), written_artifacts)
            for artifact in artifacts:
                for algorithm in ("md5", "sha256"):
                    self.assertTrue((dist_dir / f"{artifact.name}.{algorithm}").is_file())
            for manifest in ("CHECKSUMS.md5", "CHECKSUMS.sha256"):
                self.assertTrue((dist_dir / manifest).is_file())
            supply_chain_verify.verify_dist_checksums(dist_dir)

    def test_distribution_verification_rejects_artifact_and_sidecar_tampering(self):
        for artifact_suffix in (".whl", ".tar.gz"):
            for tamper_target in ("artifact", "md5", "sha256"):
                with self.subTest(artifact=artifact_suffix, target=tamper_target), tempfile.TemporaryDirectory() as temporary_directory:
                    dist_dir = Path(temporary_directory) / "dist"
                    artifacts = _write_synthetic_distribution(dist_dir)
                    supply_chain_verify.write_dist_checksums(dist_dir)
                    artifact = next(path for path in artifacts if path.name.endswith(artifact_suffix))

                    if tamper_target == "artifact":
                        artifact.write_bytes(artifact.read_bytes() + b"tampered")
                    else:
                        sidecar = dist_dir / f"{artifact.name}.{tamper_target}"
                        digest, separator, remainder = sidecar.read_text(encoding="ascii").partition("  ")
                        changed_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
                        sidecar.write_text(changed_digest + separator + remainder, encoding="ascii")

                    with self.assertRaises(ValueError):
                        supply_chain_verify.verify_dist_checksums(dist_dir)

    def test_distribution_verification_rejects_wheel_record_tampering(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            dist_dir = Path(temporary_directory) / "dist"
            wheel_path, _source_path = _write_synthetic_distribution(dist_dir)
            supply_chain_verify.write_dist_checksums(dist_dir)

            _rewrite_wheel_record(wheel_path)
            _refresh_distribution_checksums(dist_dir)

            with self.assertRaisesRegex(ValueError, "RECORD verification failed"):
                supply_chain_verify.verify_dist_checksums(dist_dir)
            with self.assertRaisesRegex(ValueError, "RECORD verification failed"):
                supply_chain_verify.write_dist_checksums(dist_dir)


class DependabotCooldownContractTests(unittest.TestCase):
    def test_both_dependency_ecosystems_have_a_five_day_cooldown(self):
        config = (REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertRegex(config, r"(?m)^version:[ \t]*2[ \t]*$")

        entries = list(re.finditer(r"(?m)^  - package-ecosystem:[ \t]*[\"']?([^\"'\s]+)[\"']?[ \t]*$", config))
        self.assertEqual({"pip", "github-actions"}, {entry.group(1) for entry in entries})
        for index, entry in enumerate(entries):
            end = entries[index + 1].start() if index + 1 < len(entries) else len(config)
            block = config[entry.start():end]
            with self.subTest(ecosystem=entry.group(1)):
                self.assertRegex(block, r"(?m)^    cooldown:[ \t]*\n      default-days:[ \t]*5[ \t]*$")


class WorkflowSecurityContractTests(unittest.TestCase):
    def test_workflows_use_explicit_permissions_and_immutable_action_shas(self):
        workflow_paths = (
            REPO_ROOT / ".github/workflows/dependabot-automerge.yml",
            REPO_ROOT / ".github/workflows/ci.yml",
        )
        for workflow_path in workflow_paths:
            workflow = workflow_path.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow_path.name):
                self.assertRegex(workflow, r"(?m)^permissions:[ \t]*$")
                references = re.findall(r"(?m)^\s*uses:[ \t]*([^\s#]+)", workflow)
                self.assertTrue(references)
                for reference in references:
                    self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_dependabot_automerge_is_restricted_and_uses_metadata(self):
        workflow = (REPO_ROOT / ".github/workflows/dependabot-automerge.yml").read_text(encoding="utf-8")

        self.assertIn("dependabot[bot]", workflow)
        self.assertIn("dependabot/fetch-metadata", workflow)
        self.assertIn("gh pr merge --auto", workflow)

    def test_dependabot_verifies_manifests_before_automerge_without_pushing_commits(self):
        workflow = (REPO_ROOT / ".github/workflows/dependabot-automerge.yml").read_text(encoding="utf-8")

        self.assertIn("python supply_chain_verify.py --verify-manifests", workflow)
        self.assertLess(workflow.index("--verify-manifests"), workflow.index("gh pr merge --auto"))
        self.assertNotIn("--update-manifests", workflow)
        self.assertNotIn("git commit", workflow)
        self.assertNotIn("git push", workflow)

    def test_ci_verifies_sha_supply_chain_tests_and_trust_files(self):
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertRegex(workflow, r"git rev-parse HEAD.*\$\{GITHUB_SHA\}")
        self.assertIn("supply_chain_verify.py --verify-manifests", workflow)
        manifest_step = workflow.split("- name: Verify dependency pins and manifests", 1)[1].split("- name:", 1)[0]
        self.assertNotIn("if:", manifest_step)
        self.assertIn("pip install --require-hashes -r build-requirements.txt", workflow)
        self.assertIn("python -m unittest", workflow)
        self.assertIn("supply_chain_verify.py --build-and-verify dist", workflow)
        self.assertIn("supply_chain_verify.py --verify-dist dist", workflow)
        self.assertIn("sha256sum --check CHECKSUMS.sha256", workflow)
        self.assertIn("md5sum --check CHECKSUMS.md5", workflow)
        for trust_file in ("dist/*.md5", "dist/*.sha256", "dist/CHECKSUMS.md5", "dist/CHECKSUMS.sha256"):
            self.assertIn(trust_file, workflow)


if __name__ == "__main__":
    unittest.main()
