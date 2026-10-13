"""Deterministic checks for the native headless request and disclosure boundary."""

import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import headless


class HeadlessImportTests(unittest.TestCase):
    def test_import_does_not_load_qt_or_gui_modules(self):
        self.assertNotIn("PyQt5", sys.modules)
        self.assertNotIn("GUI.GuiManager.GuiBehavior", sys.modules)


class HeadlessContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        (self.root / "ontology/.ontology-sessions/run-1/ontogenix").mkdir(parents=True, mode=0o700)
        self.session = self.root / "ontology/.ontology-sessions/run-1/ontogenix"
        self.source = self.root / "data.csv"
        self.source_bytes = b"id,label\n1,private-value-one\n2,private-value-two\n"
        self.source.write_bytes(self.source_bytes)
        self.source_digest = hashlib.sha256(self.source_bytes).hexdigest()
        self.request = {
            "contract_version": 1,
            "run_id": "run-1",
            "session_output_path": "ontology/.ontology-sessions/run-1/ontogenix",
            "operation": "bottom-up-candidate",
            "approved_scope_ref": "owner-handoff-1",
            "approved_cq_ids": ["cq-1"],
            "source": {"source_id": "dataset-1", "path": "data.csv", "sha256": self.source_digest},
            "project": {
                "ontology_iri": "https://example.test/ontology",
                "prefix": "ex",
                "target_profile": "OWL2_EL",
            },
            "disclosure_authorization": {
                "authorization_ref": "consent-1",
                "authorized_by": "caller-1",
                "purpose": headless.PROMPT_PURPOSE,
                "model_alias": "configured/model",
                "source_refs": [{"source_id": "dataset-1", "sha256": self.source_digest}],
                "max_requests": 4,
                "max_input_bytes_per_request": 65536,
                "max_total_input_bytes": 262144,
                "max_output_tokens_total": 4096,
                "deadline_seconds": 300,
            },
        }

    def test_valid_request_binds_existing_private_directory_and_source(self):
        bound = headless.validate_request(
            self.request, str(self.session), self.root
        )
        self.assertEqual(self.source, bound["source_path"])
        self.assertEqual(self.session, bound["output_path"])
        copied, digest = headless._read_source(self.source, self.source_digest)
        self.assertEqual(self.source_bytes, copied)
        self.assertEqual(self.source_digest, digest)

    def test_rejects_unknown_fields_and_mismatched_source_authorization(self):
        unknown = dict(self.request, extra="not permitted")
        with self.assertRaises(headless.InvalidRequest):
            headless.validate_request(unknown, str(self.session), self.root)

        mismatched = json.loads(json.dumps(self.request))
        mismatched["disclosure_authorization"]["source_refs"][0]["sha256"] = "0" * 64
        with self.assertRaises(headless.InvalidRequest):
            headless.validate_request(mismatched, str(self.session), self.root)

    def test_rejects_source_digest_mismatch_before_profile_or_dispatch(self):
        with self.assertRaises(headless.InvalidRequest):
            headless._read_source(self.source, "0" * 64)

    def test_rejects_symlinked_source_and_nonempty_output(self):
        link = self.root / "linked.csv"
        link.symlink_to(self.source)
        linked_request = json.loads(json.dumps(self.request))
        linked_request["source"]["path"] = "linked.csv"
        with self.assertRaises(headless.InvalidRequest):
            headless.validate_request(linked_request, str(self.session), self.root)

        (self.session / "existing").write_text("preserve", encoding="utf-8")
        with self.assertRaises(headless.InvalidRequest):
            headless.validate_request(self.request, str(self.session), self.root)

    def test_profile_is_metadata_only_and_limited_to_100_rows(self):
        rows = ["identifier,description"]
        rows.extend(f"{index},private-value-{index}" for index in range(101))
        profile = headless._profile_csv(("\n".join(rows) + "\n").encode("utf-8"))
        parsed = json.loads(profile)
        self.assertEqual(100, parsed["rows_profiled"])
        self.assertTrue(parsed["row_profile_truncated"])
        self.assertLessEqual(len(profile), headless.MAX_PROFILE_BYTES)
        self.assertNotIn(b"private-value", profile)
        self.assertEqual({"name", "observed_types", "non_empty_rows", "unique_values_in_profile"},
                         set(parsed["columns"][1]))

    def test_scoped_disclosure_rejects_changed_payload_and_enforces_actual_usage(self):
        messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "profile"}]
        checked = []
        policy = headless.ScopedDisclosure(
            self.request["disclosure_authorization"], "configured/model", messages,
            source_check=lambda: checked.append(True),
        )
        with self.assertRaises(PermissionError):
            policy.authorize(messages, "other purpose", "configured/model", 4096)
        self.assertEqual([], checked)
        options = policy.authorize(messages, "model request", "configured/model", 4096)
        self.assertEqual(4096, options["max_tokens"])
        self.assertGreater(options["timeout"], 0)
        self.assertEqual([True], checked)
        policy.record_completion(SimpleNamespace(usage=SimpleNamespace(completion_tokens=35)))
        self.assertEqual(1, policy.request_count)
        self.assertEqual(35, policy.output_tokens)

    def test_scoped_authorization_is_applied_before_the_native_model_call(self):
        openai_stub = types.ModuleType("openai")
        openai_stub.OpenAI = object
        openai_stub.AzureOpenAI = object
        with patch.dict(sys.modules, {"openai": openai_stub}):
            llm_module = importlib.import_module("GUI.LLM_base.LlmBase")
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="@prefix ex: <https://example.test/> ."))],
            usage=SimpleNamespace(completion_tokens=12),
        )
        completions = Mock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completions)))
        policy = Mock()
        policy.authorize.return_value = {"max_tokens": 256, "timeout": 9.0}
        llm = llm_module.AbstractLlm({
            "client": "openai", "model": "configured/model", "role": "fixed system prompt",
            "seed": None, "scoped_authorization": policy,
        })
        with patch.object(llm_module, "OpenAI", return_value=client), patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            answer = llm.get_api_response("bounded profile", temperature=0, max_tokens=4096)
        self.assertTrue(answer.startswith("@prefix"))
        self.assertEqual(256, completions.call_args.kwargs["max_tokens"])
        self.assertEqual(9.0, completions.call_args.kwargs["timeout"])
        policy.authorize.assert_called_once()
        policy.record_completion.assert_called_once_with(response)

    def test_native_pipeline_writes_only_bounded_session_artifacts(self):
        previous_cwd = Path.cwd()
        os.chdir(self.root)
        try:
            metadata = SimpleNamespace(
                api_model="configured/model",
                onto_metadata={
                    "model": "configured/model", "role": "fixed system prompt", "client": "openai", "seed": None,
                },
            )
            instructions = "Use only this data profile:\n{json_data}\n{data_description}"
            resource_digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()

            class FakeLlm:
                def __init__(self, llm_metadata):
                    self.role = llm_metadata["role"]
                    self.model = llm_metadata["model"]
                    self.scoped_authorization = llm_metadata["scoped_authorization"]

                def get_api_response(self, prompt, temperature, max_tokens):
                    messages = [
                        {"role": "system", "content": self.role},
                        {"role": "user", "content": prompt},
                    ]
                    options = self.scoped_authorization.authorize(
                        messages, "model request", self.model, max_tokens
                    )
                    self.assert_budget(options)
                    self.scoped_authorization.record_completion(
                        SimpleNamespace(usage=SimpleNamespace(completion_tokens=25))
                    )
                    return "@prefix ex: <https://example.test/> .\n"

                @staticmethod
                def assert_budget(options):
                    if options["max_tokens"] != 4096 or options["timeout"] <= 0:
                        raise AssertionError("scoped limits were not applied")

            native_llm_module = types.ModuleType("GUI.LLM_base.LlmBase")
            native_llm_module.AbstractLlm = FakeLlm
            with (
                patch.dict(sys.modules, {"GUI.LLM_base.LlmBase": native_llm_module}),
                patch.object(headless, "_load_model_resources", return_value=(metadata, instructions, resource_digest)),
            ):
                result, result_bytes = headless.run_request(json.dumps(self.request), str(self.session))
        finally:
            os.chdir(previous_cwd)

        self.assertEqual({"input-copy.csv", "data-profile.json", "candidate.ttl", "result.json"},
                         {path.name for path in self.session.iterdir()})
        self.assertEqual(self.source_bytes, (self.session / "input-copy.csv").read_bytes())
        self.assertEqual(0o600, (self.session / "input-copy.csv").stat().st_mode & 0o777)
        self.assertEqual(result_bytes, (self.session / "result.json").read_bytes())
        self.assertEqual("candidate", result["status"])
        self.assertEqual(1, result["provenance"]["model_request_count"])
        self.assertEqual(self.source_digest, result["provenance"]["source_sha256"])
        self.assertNotIn(b"private-value", (self.session / "data-profile.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
