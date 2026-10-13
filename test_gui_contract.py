"""Deterministic stdlib checks for model routing and generated-action consent."""

import ast
import asyncio
import configparser
import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


# The source contract needs only these client names at import time. Keep this
# test runnable without installing the application's dependencies on the host.
openai_stub = types.ModuleType("openai")
openai_stub.OpenAI = object
openai_stub.AzureOpenAI = object
with patch.dict(sys.modules, {"openai": openai_stub}):
    LlmBaseModule = importlib.import_module("GUI.LLM_base.LlmBase")
    AbstractLlm = LlmBaseModule.AbstractLlm


class DatasetPathContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dependencies = {}
        if importlib.util.find_spec("chardet") is None:
            chardet_stub = types.ModuleType("chardet")
            chardet_stub.UniversalDetector = object
            dependencies["chardet"] = chardet_stub
        with patch.dict(sys.modules, dependencies):
            cls.metadata_module = importlib.import_module("GUI.GuiManager.metadata")

    def test_config_creation_preserves_absolute_session_path_outside_caller_cwd(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            caller = root / "caller"
            session = root / "private session"
            caller.mkdir()
            session.mkdir(mode=0o700)
            dataset = session / "fixture 日本語.csv"
            original = b"id,label\n1,alpha\n2,beta\n"
            dataset.write_bytes(original)
            previous_cwd = Path.cwd()
            os.chdir(caller)
            try:
                with patch.object(
                    self.metadata_module.filepath_tools,
                    "check_file_encoding_mime_type",
                    return_value=("utf-8", "text/csv"),
                ):
                    manager = self.metadata_module.MetadataManager()
                    manager.create_config4morph(str(dataset))
                metadata = manager.dataset_metadata
                self.assertEqual(Path(metadata.dataset_full_path), dataset)
                self.assertEqual(Path(metadata.dataset_config_path), session / "config.ini")
                configuration = configparser.ConfigParser()
                configuration.read(metadata.dataset_config_path, encoding="utf-8")
                self.assertEqual(Path(configuration["DataSourceCSV"]["file_path"]), dataset)
                self.assertEqual(dataset.read_bytes(), original)
                self.assertEqual(list(caller.iterdir()), [])
            finally:
                os.chdir(previous_cwd)


class ModelRequestContractTests(unittest.TestCase):
    def metadata(self, confirm, generated=None):
        return {
            "client": "openai",
            "model": "organization/custom-alias",
            "role": "neutral",
            "seed": 123,
            "confirm_external_request": confirm,
            "confirm_generated_action": generated,
        }

    def test_custom_alias_and_base_url_reach_client_after_consent(self):
        with patch.object(LlmBaseModule, "OpenAI") as openai:
            client = openai.return_value
            client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="candidate"))]
            )
            confirm = Mock(return_value=True)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "http://gateway.test/v1"}):
                llm = AbstractLlm(self.metadata(confirm))
                self.assertIsNone(llm.client)
                self.assertEqual("candidate", llm.get_api_response("independent profile"))
            openai.assert_called_once_with(api_key="test-key", base_url="http://gateway.test/v1")
            self.assertEqual("organization/custom-alias", client.chat.completions.create.call_args.kwargs["model"])
            self.assertIn("independent profile", str(confirm.call_args))

    def test_declined_disclosure_makes_no_client_or_request(self):
        with patch.object(LlmBaseModule, "OpenAI") as openai:
            llm = AbstractLlm(self.metadata(lambda *_: False))
            with self.assertRaises(PermissionError):
                llm.get_api_response("private profile")
            openai.assert_not_called()
            self.assertIsNone(llm.client)



class NativeModelSelectionTests(unittest.TestCase):
    @staticmethod
    def metadata_manager(arguments=None, environment=None):
        chardet_stub = types.ModuleType("chardet")
        chardet_stub.UniversalDetector = object
        module_context = patch.dict(sys.modules, {"chardet": chardet_stub})
        module_context.start()
        try:
            metadata_module = importlib.import_module("GUI.GuiManager.metadata")
            with patch.dict(os.environ, environment or {}, clear=False):
                if arguments is None:
                    arguments = SimpleNamespace(api_model=None, client="openai", mapping_extension="ttl", ssl_cert=None)
                return metadata_module.MetadataManager(arguments)
        finally:
            module_context.stop()

    def test_cli_model_alias_overrides_environment(self):
        manager = self.metadata_manager(
            SimpleNamespace(api_model="operator/model-v2", client="openai", mapping_extension="ttl", ssl_cert=None),
            {"ONTOGENIX_API_MODEL": "configured/alias", "OPENAI_MODEL": "secondary/alias"},
        )
        self.assertEqual("operator/model-v2", manager.api_model)

    def test_dedicated_config_precedes_secondary_env_and_empty_is_rejected(self):
        manager = self.metadata_manager(environment={"ONTOGENIX_API_MODEL": "configured/alias", "OPENAI_MODEL": "secondary/alias"})
        self.assertEqual("configured/alias", manager.api_model)
        with self.assertRaises(ValueError):
            self.metadata_manager(environment={"ONTOGENIX_API_MODEL": ""})


class OptionalSearchContractTests(unittest.TestCase):
    def test_search_is_disabled_without_key_and_fails_closed_when_enabled(self):
        serpapi_stub = types.ModuleType("serpapi")
        serpapi_stub.Client = Mock()
        with patch.dict(sys.modules, {"serpapi": serpapi_stub}):
            search_module = importlib.import_module("GUI.PlanSage.RAG_google")
            with patch.dict(os.environ, {"ONTOGENIX_ENABLE_WEB_SEARCH": "", "SERPAPI_API_KEY": ""}):
                searcher = search_module.Searcher({})
                self.assertEqual({}, searcher._search_schema_org("Person"))
            with patch.dict(os.environ, {"ONTOGENIX_ENABLE_WEB_SEARCH": "1", "SERPAPI_API_KEY": ""}):
                enabled = search_module.Searcher({})
                with self.assertRaises(ValueError):
                    enabled._search_schema_org("Person")
        serpapi_stub.Client.assert_not_called()


class GeneratedActionConsentTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def tool_call():
        return SimpleNamespace(function=SimpleNamespace(name="generate_candidate", arguments='{"scope":"independent"}'))

    async def test_action_runs_only_after_explicit_confirmation(self):
        calls = []
        confirmations = []
        llm = AbstractLlm({
            "client": "openai", "model": "default", "role": "neutral", "seed": None,
            "confirm_generated_action": lambda name, args: confirmations.append((name, args)) or True,
        })
        llm.available_functions = {"generate_candidate": lambda **args: self.record(calls, args)}
        llm.tool_calls = [self.tool_call()]

        await llm._process_function_response()

        self.assertEqual([("generate_candidate", {"scope": "independent"})], confirmations)
        self.assertEqual([{"scope": "independent"}], calls)

    async def test_cancellation_while_model_consent_is_open_prevents_client_creation(self):
        with patch.object(LlmBaseModule, "OpenAI") as openai:
            def cancel_during_consent(*_):
                asyncio.current_task().cancel()
                return True

            llm = AbstractLlm({
                "client": "openai", "model": "default", "role": "neutral", "seed": None,
                "confirm_external_request": cancel_during_consent,
            })

            async def request():
                llm.get_api_response("private profile")

            task = asyncio.create_task(request())
            with self.assertRaises(asyncio.CancelledError):
                await task
            openai.assert_not_called()

    async def test_declined_or_missing_confirmation_has_no_action_side_effect(self):
        for confirmation in (lambda *_: False, None):
            calls = []
            llm = AbstractLlm({
                "client": "openai", "model": "default", "role": "neutral", "seed": None,
                "confirm_generated_action": confirmation,
            })
            llm.available_functions = {"generate_candidate": lambda **args: self.record(calls, args)}
            llm.tool_calls = [self.tool_call()]

            await llm._process_function_response()
            self.assertEqual([], calls)

    async def test_cancellation_during_confirmation_prevents_dispatch(self):
        calls = []
        llm = AbstractLlm({"client": "openai", "model": "default", "role": "neutral", "seed": None})
        llm.available_functions = {"generate_candidate": lambda **args: self.record(calls, args)}
        llm.tool_calls = [self.tool_call()]

        def cancel_current_task(*_):
            current = asyncio.current_task()
            current.cancel()
            return True

        llm.confirm_generated_action = cancel_current_task
        dispatch = asyncio.create_task(llm._process_function_response())
        try:
            await dispatch
        except asyncio.CancelledError:
            pass
        self.assertEqual([], calls)

    @staticmethod
    async def record(calls, arguments):
        calls.append(arguments)


class GuiEntryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path("GUI/GuiManager/GuiBehavior.py").read_text(encoding="utf-8"))
        class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GuiBehavior")
        cls.methods = {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}

    def test_startup_and_reset_do_not_start_a_model_action(self):
        for name in ("__init__", "_reset_context"):
            with self.subTest(method=name):
                calls = [
                    node.func.attr
                    for node in ast.walk(self.methods[name])
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                ]
                self.assertNotIn("_start_manage_action", calls)

    def test_action_entry_has_an_in_flight_guard_before_scheduling(self):
        method = self.methods["_start_manage_action"]
        schedule_index = next(
            index for index, node in enumerate(method.body)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "ensure_future"
        )
        self.assertTrue(any(isinstance(node, ast.If) for node in method.body[:schedule_index]))


@unittest.skipUnless(importlib.util.find_spec("rdflib") and importlib.util.find_spec("PyQt5"), "requires project RDFLib/PyQt5 runtime dependencies")
class CandidateSerializationTests(unittest.TestCase):
    def test_rdfxml_serializes_current_edited_turtle(self):
        from rdflib import Graph
        from GUI.OntologyManager.OntologyManager import OntologyManager

        edited = """@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:CurrentCandidate a owl:Ontology ; owl:versionInfo \"edited\" .
"""
        serialized = OntologyManager.serialize_candidate_text(edited, "xml")
        restored = Graph().parse(data=serialized, format="xml")
        expected = Graph().parse(data=edited, format="turtle")
        self.assertTrue(restored.isomorphic(expected))


@unittest.skipUnless(importlib.util.find_spec("PyQt5"), "requires project PyQt5 runtime dependencies")
class CsvLoadingContractTests(unittest.TestCase):
    def setUp(self):
        from PyQt5.QtWidgets import QApplication
        from GUI.GuiManager.GuiBehavior import GuiBehavior

        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_loop = None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def restore_loop():
            loop.close()
            asyncio.set_event_loop(previous_loop)

        self.addCleanup(restore_loop)
        self.app = QApplication.instance() or QApplication([])
        arguments = SimpleNamespace(api_model="test-alias", client="openai", mapping_extension="ttl", ssl_cert=None)
        self.gui = GuiBehavior(arguments)

        def close_gui():
            self.gui.timer.stop()
            self.gui.close()
            self.app.processEvents()

        self.addCleanup(close_gui)

    def test_csv_load_creates_private_config_and_profile_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            caller = root / "caller"
            session = root / "private session"
            caller.mkdir()
            session.mkdir(mode=0o700)
            source = caller / "fixture 日本語.csv"
            original = b"id,label\n1,alpha\n2,beta\n"
            source.write_bytes(original)
            source.chmod(0o444)
            caller.chmod(0o555)
            previous_cwd = Path.cwd()
            os.chdir(caller)
            try:
                with (
                    patch.dict(os.environ, {"ONTOGENIX_SESSION_DIR": str(session)}),
                    patch.object(self.gui, "open_file_dialog", return_value=str(source)),
                ):
                    self.gui.load_csv_data()
                metadata = self.gui.metadata_manager.dataset_metadata
                working_copy = Path(metadata.dataset_full_path)
                self.assertTrue(working_copy.is_absolute())
                self.assertTrue(working_copy.is_relative_to(session))
                self.assertNotEqual(working_copy, source)
                self.assertEqual(Path(metadata.dataset_config_path).parent, working_copy.parent)
                self.assertTrue(Path(metadata.dataset_config_path).is_file())
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(list(caller.iterdir()), [source])
                self.assertTrue(self.gui.json_data)
                profile = self.gui.csv_textedit.toPlainText()
                self.assertIn("id", profile)
                self.assertIn("label", profile)
                self.assertTrue(self.gui.save_json_btn.isEnabled())
                components = (self.gui.genie, self.gui.prompt_crafter, self.gui.plan_builder,
                              self.gui.ontology_builder, self.gui.ontology_mapper, self.gui.mermaid_generator)
                self.assertTrue(all(component.client is None for component in components))
                self.assertIsNone(self.gui.current_task)
            finally:
                os.chdir(previous_cwd)
                caller.chmod(0o755)

    def test_config_io_error_is_reported_without_escaping_the_qt_callback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "fixture.csv"
            source.write_text("id,label\n1,alpha\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"ONTOGENIX_SESSION_DIR": str(root / "sessions")}),
                patch.object(self.gui, "open_file_dialog", return_value=str(source)),
                patch.object(self.gui.metadata_manager, "create_config4morph", side_effect=FileNotFoundError("config.ini")),
                patch.object(self.gui.log, "append_log") as log,
            ):
                self.gui.load_csv_data()
            self.assertTrue(any(call.kwargs.get("level") == "error" for call in log.call_args_list))
            self.assertIsNone(self.gui.json_data)
            self.assertFalse(self.gui.save_json_btn.isEnabled())
            self.assertTrue(self.gui.isVisible())


@unittest.skipUnless(importlib.util.find_spec("PyQt5"), "requires project PyQt5 runtime dependencies")
class QtStartupContractTests(unittest.TestCase):
    def test_gui_startup_and_reset_construct_no_model_clients(self):
        from PyQt5.QtWidgets import QApplication
        from GUI.GuiManager.GuiBehavior import GuiBehavior

        app = QApplication.instance() or QApplication([])
        arguments = SimpleNamespace(api_model=None, client="openai", mapping_extension="ttl", ssl_cert=None)
        gui = GuiBehavior(arguments)
        components = (gui.genie, gui.prompt_crafter, gui.plan_builder, gui.ontology_builder,
                      gui.ontology_mapper, gui.mermaid_generator)
        self.assertTrue(all(component.client is None for component in components))
        self.assertIsNone(gui.current_task)

        gui._reset_context()
        components = (gui.genie, gui.prompt_crafter, gui.plan_builder, gui.ontology_builder,
                      gui.ontology_mapper, gui.mermaid_generator)
        self.assertTrue(all(component.client is None for component in components))
        self.assertIsNone(gui.current_task)
        gui.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
