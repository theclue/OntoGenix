
"""
    Main module for the application GUI.
"""
# Standard and third-party libraries
import asyncio
import base64
import json
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from rdflib.plugins.parsers.notation3 import BadSyntax

from PyQt5.QtWidgets import QMainWindow, QFileDialog, QDialog, QVBoxLayout, QTextEdit, QAction, QLabel, QPlainTextEdit, QDialogButtonBox, QMessageBox # pylint: disable = no-name-in-module, line-too-long
from PyQt5.QtCore import QTimer, QCoreApplication, Qt # pylint: disable = no-name-in-module
from PyQt5.QtGui import QPixmap, QTextCursor # pylint: disable = no-name-in-module
from PyQt5 import uic


# Internal modules imports
from GUI.GuiManager.metadata import MetadataManager
from GUI.GuiManager.log import Log
from GUI.tools.tools import dataframe2prettyjson, csv_data_preprocessing, find_unique_identifier
from GUI.GuiManager.LLM_Genie import Genie
from GUI.PromptCrafter.LLM_promptCrafter import LlmPromptCrafter
from GUI.PlanSage.LLM_planner import LlmPlanner
from GUI.OntoBuilder.LLM_ontology import LlmOntology
from GUI.OntoMapper.LLM_ontomapper import LlmOntoMapper
from GUI.MermaidOntoFlow.Llm_mermaid import LlmMermaid
from GUI.KG_Generator.RAG import RAG_OntoMapper
#from GUI.OntologyManager.OntologyManager import OntologyManager
from GUI.OntoGenixExceptions.AutomataException import (InvalidTransitionException,
                                                       MissedPreviousStepException)


class GuiBehavior(QMainWindow):
    """Main GUI behavior and logic."""

    def __init__(self,  *argv):
        super().__init__(parent=None)
        gui_root = Path(__file__).resolve().parents[1]
        uic.loadUi(str(gui_root / "GUI.ui"), self)

        # Initial GUI Setup
        self.showMaximized()
        self.OUTPUT_tab.setCurrentIndex(0)
        self.LLManswer_textedit.setReadOnly(True)
        self.LLManswer_textedit.setEnabled(False)
        self.csv_textedit.setReadOnly(True)

        # Setup asyncio event loop with Qt
        self.loop : asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self.timer : QTimer = QTimer(self)
        self.timer.timeout.connect(self._run_asyncio_loop)
        self.timer.start(1000) # 1 second timeout
        #
        self.current_task = None
        self.isStopped = False
        self._context_revision = 0

        # Contextual top menu buttons
        self._createActions()
        self._createMenuBar()
        self._connectActions()
        # Manage button actions
        self._manage_gui_actions()

        # Data containers
        self.json_data = None
        # Log manager setup
        self.log : Log = Log(self.LLManswer_textedit)

        # Update context from metadata information. TODO: LLMAgentManager()
        self.metadata_manager : MetadataManager = MetadataManager(argv[0])
        self._update_context(self.metadata_manager)

        self.log.append_log("Choose an action when ready. No model request is sent at startup.", level="info", end="\n")

    def keyPressEvent(self, event): # pylint: disable = invalid-name
        """
            Overrides QtWidget.keyPressEvent(). This methods listens and handles any keyboard event.
            TODO: disable buttons while LLM is answering
            NOTE: https://stackoverflow.com/questions/60123335/correct-handling-of-keyevent-in-pyqt5-problem-with-catching-keypressevent?rq=3
            Args:
                event: Pressed keyboard key.
        """
        keyboard_map = {
            Qt.Key_Escape: ("Esc", self.stopAction.trigger),
            Qt.Key_F2: ("F2", self.resetAction.trigger)
        }
        try:
            keyboard_map.get(event.key())[1]()
        except: # pylint: disable = bare-except
            #print("El boton no está mapeado")
            pass

    def _manage_gui_actions(self):
        """
            Method where all the buttons from the UI are condensed.
        """
        # Query button callback <widget class="QPushButton">
        self.action_comboBox.addItems([
            #"Let's propose an ontology design based on the given dataset", # (!) another iteration of the message below
            "Let's propose an area of study for the given dataset to ensure the prompt-crafting step", # NOTE: Doesn't work with openai-api-version < 4
            "Let's generate the high-level structure for the provided data",
            "Let's generate the ontology from the high-level structure",
            "Let's define the entity for the ontology with extra data", #"Let's enrich the ontology with extra data",
            "Let's generate the mappings from the ontology"
        ])
        self.query_send_btn.clicked.connect(
            lambda: self._start_manage_action(self.action_comboBox.currentText())
        )
        self.attach_btn.clicked.connect(self.attach_file)
        # CSV inspector tab callbacks
        self.load_csv_btn.clicked.connect(self.load_csv_data)
        self.save_json_btn.clicked.connect(
            lambda: self.save(self.csv_textedit.toPlainText(), extension="json")
        )
            # (!) Disable button for saving
        self.save_json_btn.setEnabled(False)
        self.csv_textedit.textChanged.connect(self.on_data_changed)
        # Description tab callbacks
        self.description_save_btn.clicked.connect(
            lambda: self.save(self.description_textEdit.toPlainText(), extension="txt")
        )
        self.description_textEdit.textChanged.connect(
            lambda: self.on_text_changed(self.plan_builder, self.description_textEdit)
        )
        # Ontology tab callbacks
        self.ontology_save_btn.clicked.connect(self.save_candidate_ontology)
        self.ontology_update_btn.clicked.connect(self.onto_manager.update_text)
        self.onto_manager.textChanged.connect(
            lambda: self.on_text_changed(self.ontology_builder, self.onto_manager)
        )
        # Ontology tab callbacks - Mermaid button
        self.mermaid_btn.clicked.connect(self.create_mermaid)
        # Mapping tab callbacks
        self.mapping_save_btn.clicked.connect(
            lambda: self.save(self.mapping_textedit.toPlainText(),
                              extension=self.metadata_manager.mapping_extension) # "rml"
        )
        self.mapping_textedit.textChanged.connect(
            lambda: self.on_text_changed(self.ontology_mapper, self.mapping_textedit)
        )
        self.mapping_kgen_btn.clicked.connect(
            lambda: self.create_tripplets()
        )

    def _createActions(self):
        # # Creating action using the first constructor
        # self.newAction = QAction(self)
        # self.newAction.setText("&New")
        # # Creating actions using the second constructor
        # self.openAction = QAction("&Open...", self)
        # self.saveAction = QAction("&Save", self)
        # self.exitAction = QAction("&Exit", self)
        # self.copyAction = QAction("&Copy", self)
        # self.pasteAction = QAction("&Paste", self)
        # self.cutAction = QAction("&Cut", self)
        self.stopAction = QAction("&Stop Genix [Esc]", self)
        self.resetAction = QAction("&Reset Context [F2]", self)
        self.helpAction = QAction("&Help", self)
        self.aboutAction = QAction("&About", self)

    def _createMenuBar(self):
        """
        Creates a Menu Bar for the PyQt App.

        Example: (https://realpython.com/python-menus-toolbars/)
            menuBar = self.menuBar()
            # Creating menus using a QtWidgets.QMenu object
            testMenu = QMenu("&Test", self)
            menuBar.addMenu(testMenu)
            # Creating menus using a title
            test2Menu = menuBar.addMenu("&Test2")
            # Using an icon and a title
            import qrc_resources # For this case we need to use a QRT file
            helpMenu = menuBar.addMenu(QIcon(":help-content.svg"), "&Help") 
        """
        # QMenu object
        menuBar = self.menuBar()
        # Help menu
        fileMenu = menuBar.addMenu("&File")
        fileMenu.addAction(self.stopAction)
        fileMenu.addAction(self.resetAction)
        # Help menu
        #helpMenu = menuBar.addMenu("&Help")
        #helpMenu.addAction(self.helpAction)
        #helpMenu.addAction(self.aboutAction)

    def _connectActions(self):
        # # Connect File actions
        # self.newAction.triggered.connect(self.newFile)
        # self.openAction.triggered.connect(self.openFile)
        # self.saveAction.triggered.connect(self.saveFile)
        # self.exitAction.triggered.connect(self.close)
        self.stopAction.triggered.connect(self._stop_manage_action)
        self.resetAction.triggered.connect(self._reset_context)
        # # Connect Edit actions
        # self.copyAction.triggered.connect(self.copyContent)
        # self.pasteAction.triggered.connect(self.pasteContent)
        # self.cutAction.triggered.connect(self.cutContent)
        # Connect Help actions
        self.helpAction.triggered.connect(
            lambda: self._show_message('Manual', 'This is the manual.')
        )
        self.aboutAction.triggered.connect(
            lambda: self._show_message('About', 'This is the about information.')
        )

    def _show_message(self, title, message):
        """
            Shows a popup titled window with the information about the message
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        #dialog.setStyleSheet(self.app_style) # QDialog is not in the QSS file
        layout = QVBoxLayout()
        label = QLabel(message)
        layout.addWidget(label)
        dialog.setLayout(layout)
        dialog.exec_()

    def _update_context(self, metadata_manager:MetadataManager=None, reset_log:bool=False):
        """
            When called, the initial context of the application is reseted to its initial state (?).
            Some variables like "json_data" and the logger variable are also reseted. (shall this be done here?)

            TODO: reset automaton (by reinitializing the LLMGenie, the automaton is reseted)
            TODO: "avaliable_functions" as parameter (maybe)
        """
        try:
            _metadata_manager = metadata_manager or self.metadata_manager
            # Instantiate supporting classes; metadata for agents. (!) This variable is initialized here.
            _metadata_manager.confirm_external_request = self._confirm_external_request
            _metadata_manager.confirm_generated_action = self._confirm_generated_action
            _metadata_manager.confirm_external_search = self._confirm_external_search
            for agent_metadata in (
                _metadata_manager.genie_metadata,
                _metadata_manager.crafter_metadata,
                _metadata_manager.planner_metadata,
                _metadata_manager.onto_metadata,
                _metadata_manager.mapper_metadata,
                _metadata_manager.mermaid_metadata,
            ):
                agent_metadata["confirm_external_request"] = self._confirm_external_request
                agent_metadata["confirm_generated_action"] = self._confirm_generated_action
                agent_metadata["confirm_external_search"] = self._confirm_external_search
            _metadata_manager.genie_metadata['available_functions'] = {
                "prompt_crafting": self.generate_crafted_prompt,
                "data_description": self.create_data_description,
                "ontology_building": self.generate_ontology,
                "ontology_entity_enrichment": self.entity_enrichment,
                "mapping": self.create_mapping
            }
            # Instantiate LLM agents
            # TODO: instead of variables, use a class to manage the Agents
            _genie : Genie = Genie(_metadata_manager.genie_metadata)
            _prompt_crafter : LlmPromptCrafter = LlmPromptCrafter(_metadata_manager.crafter_metadata)
            _plan_builder : LlmPlanner = LlmPlanner(_metadata_manager.planner_metadata)
            _ontology_builder : LlmOntology = LlmOntology(_metadata_manager.onto_metadata)
            _ontology_mapper : LlmOntoMapper = LlmOntoMapper(_metadata_manager.mapper_metadata)
            _mermaid_generator : LlmMermaid = LlmMermaid(_metadata_manager.mermaid_metadata)
            # Instantiate RAG systems
            _RAG_OntoMapper : RAG_OntoMapper = RAG_OntoMapper(_ontology_mapper, _plan_builder)

            # Update agents
            self.metadata_manager : MetadataManager = metadata_manager
            self.metadata_manager.genie_metadata['available_functions'] = metadata_manager.genie_metadata['available_functions']
            # Update instantiated LLM agents
            self.genie = _genie
            self.prompt_crafter = _prompt_crafter
            self.plan_builder = _plan_builder
            self.ontology_builder = _ontology_builder
            self.ontology_mapper = _ontology_mapper
            self.mermaid_generator = _mermaid_generator
            # Instantiate RAG systems
            self.RAG_OntoMapper = _RAG_OntoMapper

            ### Other variables
            # Data containers
            self.json_data = None
            # Log manager setup
            if reset_log or not hasattr(self, "log"):
                self.log : Log = self.log if hasattr(self, "log") else Log(self.LLManswer_textedit)
        except Exception as e:
            self.log.append_log(message="\nSomething went wrong. Couldn't \"clear\" current context", level="error", end="\n")
            print(e)
            return False # TODO: or raise exception
        # Everything could be updated without any trouble being found
        return True

    def _reset_context(self):
        """
            When called, everything is reset to the initial state of the application.

            TODO: reset automaton
            TODO: F2: https://stackoverflow.com/questions/60059010/binding-a-function-to-a-key-in-python-working-with-gui
        """
        self._context_revision += 1
        if self.current_task is not None and not self.current_task.done():
            self.isStopped = True
            self.current_task.cancel()
        # Set Output Tab to first textedit tab
        self.OUTPUT_tab.setCurrentIndex(0)
        # List of all "QTextEdit" objects
        qTextEditList : list(QTextEdit) = self.findChildren(QTextEdit) # type: ignore
        # Disable all current textedit objects and clear them
        for q in qTextEditList:
            q.setEnabled(False)
            q.clear()

        # We reset the context to its initial state
        self._update_context(self.metadata_manager)
        # Reset timer (?)
        #self.timer.start(0)

        # Enable them again
        for q in qTextEditList:
            q.setEnabled(True)
            
        self.LLManswer_textedit.insertPlainText("************* RESETED CONTEXT *************")
        self.isStopped = False
        self.log.append_log("Context reset. No model request was sent.", level="info", end="\n")

    @staticmethod
    def on_text_changed(agent, text_edit):
        """ Update the agents internal "answer" variable with the current text """
        # NOTE: This method is triggered on the text_edit_agent every time a chunk is yielded [NOT GOOD]
        agent.answer = text_edit.toPlainText()

    def on_data_changed(self):
        """ Update the app internal "json_data" variable with the current text """
        self.json_data = self.csv_textedit.toPlainText()
        # Enable button for saving
        self.save_json_btn.setEnabled(True)
    
    def _confirm_external_request(self, model, endpoint, messages, purpose):
        context_revision = self._context_revision
        if endpoint:
            try:
                parsed_endpoint = urlsplit(endpoint)
                host = parsed_endpoint.hostname or "operator-configured host"
                port = f":{parsed_endpoint.port}" if parsed_endpoint.port else ""
                endpoint_label = f"{parsed_endpoint.scheme or 'https'}://{host}{port}"
            except ValueError:
                endpoint_label = "operator-configured endpoint (details hidden)"
        else:
            endpoint_label = "OpenAI API default endpoint"
        display_messages = []
        image_previews = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if item.get("type") == "image_url":
                        image_data = item.get("image_url", "")
                        if isinstance(image_data, dict):
                            image_data = image_data.get("url", "")
                        if isinstance(image_data, str) and ";base64," in image_data:
                            image = QPixmap()
                            image.loadFromData(base64.b64decode(image_data.split(";base64,", 1)[1]))
                            if not image.isNull():
                                image_previews.append(image)
                        parts.append("[attached image shown below and will be sent]")
                    else:
                        parts.append(item.get("text", ""))
                content = "\n".join(parts)
            display_messages.append({"role": message.get("role"), "content": content})
        dialog = QDialog(self)
        dialog.setWindowTitle("Confirm external model request")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            f"Send the following prompt/context to model {model!r} at {endpoint_label} for {purpose}? "
            "The provider will receive this content. Declining sends nothing."
        ))
        preview = QPlainTextEdit(dialog)
        preview.setReadOnly(True)
        preview.setPlainText(json.dumps(display_messages, ensure_ascii=False, indent=2))
        layout.addWidget(preview)
        for image in image_previews:
            image_label = QLabel(dialog)
            image_label.setPixmap(image.scaled(640, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            layout.addWidget(image_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return (
            dialog.exec_() == QDialog.Accepted
            and context_revision == self._context_revision
            and not self.isStopped
        )

    def _confirm_generated_action(self, action, arguments):
        context_revision = self._context_revision
        dialog = QDialog(self)
        dialog.setWindowTitle("Confirm generated action")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            f"Run the existing OntoGenix action {action!r} with these generated arguments? "
            "This confirms only this local action, not ontology scope or domain approval."
        ))
        preview = QPlainTextEdit(dialog)
        preview.setReadOnly(True)
        preview.setPlainText(json.dumps(arguments, ensure_ascii=False, indent=2))
        layout.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return (
            dialog.exec_() == QDialog.Accepted
            and context_revision == self._context_revision
            and not self.isStopped
        )

    def _confirm_external_search(self, query):
        context_revision = self._context_revision
        answer = QMessageBox.question(
            self, "Confirm web search",
            f"Send this search query to SerpApi?\n\n{query}",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes and context_revision == self._context_revision and not self.isStopped

    def _run_asyncio_loop(self):
        """Run the asyncio event loop for a short duration."""
        self.loop.call_soon_threadsafe(self.loop.stop) # Thread safe stop
        self.loop.run_forever()

    def _start_manage_action(self, prompt: str) -> None:
        """Start one user-selected action at a time."""
        if self.current_task is not None and not self.current_task.done():
            self.log.append_log("An OntoGenix action is already in progress.", level="warning", end="\n")
            return
        self.current_task : asyncio.Task = asyncio.ensure_future(self._manage_action(prompt), loop=self.loop)
        asyncio.gather(self.current_task, return_exceptions=True)

    def _stop_manage_action(self, prompt: str) -> None:
        """Stops the asynchronous management of the user's prompt."""
        self.isStopped = True
        self.log.append_log(message="\nProcess stopped/cancelled by user.", level="warning", end="\n")
        try:
            # asyncio.gather(self.current_task, return_exceptions=True)
            if self.current_task is not None: # and not self.current_task.cancelled():
                self.current_task.cancel()
                print(self.current_task.result())
                print(self.current_task.exception())
            else:
                self.current_task = None
        except Exception as e:
            print(">>>", e)

    async def _manage_action(self, prompt: str) -> None:
        """Manage the user's prompt based on the current ontology state."""
        # TODO: read about @corountine (asyncio ones) vs Future. Task is subclass of Future
        try:
            self.LLManswer_textedit.setEnabled(False)
            self.log.append_log(message="\n\n------------- GUI MANAGER ----------------", level="manager", end="\n\n")
            # TODO: separate the following chunk into a separate method (it's common to every method)
            async for chunk in self.genie.interaction(prompt=prompt):
                self.log.append_log(chunk, level="manager", end="")
                QCoreApplication.processEvents()
                if self.isStopped: break
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except InvalidTransitionException as ite:
            print(ite)
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="\nTransición inválida", level="manager", end="\n")
        finally:
            # TODO: self.genie.automata.droid.rollback_transition()
            self.isStopped = False
            self.log.append_log(message="", level="manager", end="\n")
            self.LLManswer_textedit.setEnabled(True)
            if self.current_task is asyncio.current_task():
                self.current_task = None

    async def generate_crafted_prompt(self, prompt: str) -> None:
        """Helps the user to craft the best prompt."""
        # TODO: check this method (if needs disabling)
        try:
            self.log.append_log(message="\n\n------------- GUI MANAGER ----------------", level="manager", end="\n\n")
            content = prompt + ': ' + self.query_prompt_textedit.toPlainText()

            async for chunk in self.prompt_crafter.interaction(prompt=content, json_data=self.json_data):
                self.log.append_log(chunk, level="answer", end="")
                QCoreApplication.processEvents()
                if self.isStopped: break
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            # Rollback to the previous step of the automaton
            #self.genie.automata.droid.rollback_transition()
            #self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        finally:
            self.isStopped = False
            self.log.append_log(message="", level="answer", end="\n")
            self.query_prompt_textedit.setText(self.prompt_crafter.crafted_prompt)

    async def create_data_description(self, prompt: str) -> None:
        """Create the initial context based on user's prompt. Previously it was called `create_initial_context`"""
        # We disconnect the `text_edit` listener to prevent the `on_text_changed` method from being triggered. Thus being safer to use
        self.description_textEdit.disconnect()
        #self.description_textEdit.clear()
        self.description_textEdit.setEnabled(False)
        self.OUTPUT_tab.setCurrentIndex(1)
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            self.description_textEdit.clear()
            # We try to generate the data description of the data
            content = prompt + " " + self.query_prompt_textedit.toPlainText()
            async for chunk in self.plan_builder.interaction(input_task=content, json_data=self.json_data):
                self.description_textEdit.insertPlainText(chunk)
                # Scroll to the bottom to ensure the latest message is visible
                self.description_textEdit.verticalScrollBar().setValue(
                    self.description_textEdit.verticalScrollBar().maximum()
                )
                QCoreApplication.processEvents()
            self.description_textEdit.insertPlainText("\n\n")
            # NOTE: Until here is the description for the data
            schema_description=self.plan_builder.answer
            interoperable_entities = self.plan_builder.get_from_schema(self.plan_builder.answer)
            self.description_textEdit.clear()
            # NOTE: From here is the definition of the prefixes used in the ontology
            async for chunk in self.plan_builder.update(
                    schema_description=schema_description,
                    interoperable_entities=interoperable_entities):

                self.description_textEdit.insertPlainText(chunk)
                # Scroll to the bottom to ensure the latest message is visible
                self.description_textEdit.verticalScrollBar().setValue(
                    self.description_textEdit.verticalScrollBar().maximum()
                )
                QCoreApplication.processEvents()
                if self.isStopped: break
            if not self.isStopped:
                self.log.append_log(message="\nINFO: Data description created succesfully", level="info", end="\n")
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        finally:
            self.isStopped = False
            # Now we reconnect again the listener
            self.description_textEdit.textChanged.connect(
                lambda: self.on_text_changed(self.plan_builder, self.description_textEdit)
            )
            # Re-enable the ontology text edit after processing
            self.description_textEdit.setEnabled(True)

    async def ontology_interaction(self, max_tries:int=2):
        error : str = None
        # For preventing infinite recursion, we put a maximum number of tries to generate the ontology graph
        while max_tries > 0:
            try:
                async for chunk in self.ontology_builder.interact(
                        json_data=self.json_data,
                        data_description=self.plan_builder.answer,
                        state="ONTOLOGY",
                        error=error):
                    self.onto_manager.insertPlainText(chunk)
                    # Scroll to the bottom to ensure the latest message is visible
                    self.onto_manager.verticalScrollBar().setValue(
                        self.onto_manager.verticalScrollBar().maximum()
                    )
                    QCoreApplication.processEvents()  # Process any pending GUI events
                    if self.isStopped: break
                # TODO: maybe here we should take into account the parsing errors of the graph generation
                if not self.isStopped:
                    # update the ontology graph
                    self.onto_manager.text_to_graph(self.ontology_builder.answer)
                # Everything is "good" to go
                max_tries = 0
                error = None
            except GeneratorExit as ge:
                print(f"Process stopped/cancelled by user: {ge}")
                # Rollback to the previous step of the automaton
                self.genie.automata.droid.rollback_transition()
                self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
            except SyntaxError as se:
                # An error ocurred during the parsing process and Bad syntax was encountered.
                if issubclass(type(se.msg), BadSyntax):
                    error = "".join([
                        "```python\n",
                        str(se).replace("\\n", "\n"), 
                        "\n```",
                        "\n\nThe previous error is produced for the following ontology:\n", 
                        f"```{self.metadata_manager.mapping_extension}\n", # "ttl"
                        se.msg._str.decode(encoding='utf-8', errors='strict'),
                        "\n```"]) #f"Line {se.msg.lines}: {se.msg._why}"
                self.ontology_builder.error_message = error
                self.log.append_log(message=f"\nERROR: Couldn't parse ontology. {max_tries-1} tries left", level="error", end="\n")
                # We clean the board before trying again
                if max_tries > 1:
                    self.onto_manager.clear()
            else:
                if error:
                    self.log.append_log(message="The ontology couldn't be created within the maximum amount of tries", level="error", end="\n")
            finally:
                self.isStopped = False
                # For preventing infinite recursion, we put a maximum number of tries to generate the ontology graph
                max_tries -= 1

    async def generate_ontology(self) -> None:
        """
        Asynchronously generate the ontology based on the user's prompt.

        This coroutine interacts with the ontology_builder agent to generate an ontology
        using the provided data description from the plan_builder. The process involves
        updating the onto_manager widget with the generated ontology.
        """
        # We disconnect the text_edit listener to prevent the on_text_changed method from being triggered. Thus being safer to use
        self.onto_manager.disconnect()
        # Clear the ontology text edit and disable it during processing
        #self.onto_manager.clear()
        self.onto_manager.setEnabled(False)
        self.OUTPUT_tab.setCurrentIndex(2)  # Switch to the appropriate tab in the GUI
        max_tries:int = 4 # TODO: config-parameter
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            # If there's no `data_description`, we missed the "description" step
            if not self.plan_builder.answer:
                raise MissedPreviousStepException(step="HIGH LEVEL STRUCTURE", tab=1)
            
            self.onto_manager.clear()
            # Generate the ontology using the ontology_builder agent
            await self.ontology_interaction(max_tries=max_tries)
            # TODO: if the ontology is stopped, we shouldn't be able to try to generate the graph
            if not self.isStopped:
                self.log.append_log(message="\nINFO: Candidate ontology generated", level="info", end="\n")
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except SyntaxError as se:
            _error = None
            # An error ocurred during the parsing process and Bad syntax was encountered.
            if issubclass(type(se.msg), BadSyntax):
                _error = f"Line {se.msg.lines}: {se.msg._why}"
            # NOTE: other errors from morph_kgc can be caught here
            print(_error)
        except Exception as e:
            print("[ONTOLOGY] could not generate the graph from text:", e)
        finally:
            # Now we reconnect again the listener (if any)
            self.onto_manager.textChanged.connect(
                lambda: self.on_text_changed(self.ontology_builder, self.onto_manager)
            )
            # Re-enable the ontology text edit after processing
            self.onto_manager.setEnabled(True)

    async def entity_enrichment(self, prompt: str, entity: str) -> None:
        """
        Asynchronously generate the ontology based on the user's prompt.

        This coroutine interacts with the ontology_builder agent to generate an ontology
        using the provided data description from the plan_builder. The process involves
        updating the onto_manager widget with the generated ontology.

        Args:
            :param prompt: The user query.
            :param entity: The user queried entity.
        """
        # We disconnect the text_edit listener to prevent the on_text_changed method from being triggered. Thus being safer to use
        self.onto_manager.disconnect()
        # Clear the ontology text edit and disable it during processing
        #self.onto_manager.clear()
        self.onto_manager.setEnabled(False)
        self.OUTPUT_tab.setCurrentIndex(2)  # Switch to the appropriate tab in the GUI
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            # If there's no `data_description`, we missed the "description" step
            if not self.plan_builder.answer:
                raise MissedPreviousStepException(step="HIGH LEVEL STRUCTURE", tab=1)
            # If there's no `ontology_builder`, we missed the "ontology" step
            if not self.ontology_builder.answer:
                raise MissedPreviousStepException(step="ONTOLOGY", tab=2)
            
            self.onto_manager.clear()
            # Generates the ontology using the ontology_builder agent
            # TODO: It also uses the "query_prompt_textedit" variable in the UI to pass the full prompt
            content = 'Query: ' + prompt + ': \n ' + self.query_prompt_textedit.toPlainText()
            async for chunk in self.ontology_builder.interact(
                    task=content,
                    data_description=self.plan_builder.answer, # TODO: change this prefixes for the ontology ones
                    entity=entity, # "extra data" (?)
                    state="ONTOLOGY_ENTITY"):
                self.onto_manager.insertPlainText(chunk)
                # Scroll to the bottom to ensure the latest message is visible
                self.onto_manager.verticalScrollBar().setValue(
                    self.onto_manager.verticalScrollBar().maximum()
                )
                # Process any pending GUI events
                QCoreApplication.processEvents()
                if self.isStopped: break
            # Update the ontology graph entity. TODO: check if it's correct
            self.onto_manager.entity_update(self.ontology_builder.answer)
            self.onto_manager.clear()
            self.onto_manager.insertPlainText(self.onto_manager.graph_to_text())
            if not self.isStopped:
                self.log.append_log(message="\nINFO: Ontology updated succesfully", level="info", end="\n")
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except Exception as e:
            self.log.append_log(message=f"\nCould not update the graph {e}", level="error")
            print('[ONTOLOGY_ENTITY] could not update the graph:', e)
        finally:
            self.isStopped = False
            # Now we reconnect again the listener (if any)
            self.onto_manager.textChanged.connect(
                lambda: self.on_text_changed(self.ontology_builder, self.onto_manager)
            )
            # Re-enable the ontology text edit after processing
            self.onto_manager.setEnabled(True)

    async def create_mapping(self, from_ontology:bool=True) -> None:
        """
            Create mapping using the rationale and ontology text.

            Args:
                from_ontology: Flag indicating whether to create the mapping from the ontology or from the description
        """
        max_iter : int = 0
        # We disconnect the text_edit listener to prevent the on_text_changed method from being triggered. Thus being safer to use
        self.mapping_textedit.disconnect()
        # Set GUI tab to "mapping" tab
        self.OUTPUT_tab.setCurrentIndex(4)
        self.mapping_textedit.setEnabled(False)
        print('create_mapping ', self.RAG_OntoMapper, self.RAG_OntoMapper.kgen)
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            # If there's no `data_description`, we missed the "description" step
            if not self.plan_builder.answer:
                raise MissedPreviousStepException(step="HIGH LEVEL STRUCTURE", tab=1)
            # If there's no `ontology_builder`, we missed the "ontology" step
            if not self.ontology_builder.answer and from_ontology:
                raise MissedPreviousStepException(step="ONTOLOGY", tab=2)
            # TODO: now that is separate into 2 different functions, we should change the conditions to re-run the mapping
            # While max_iter is not exceeded or the mapping isn't done
            #while max_iter < self.RAG_OntoMapper.max_iter:
            #    if (self.RAG_OntoMapper.kgen is None or self.RAG_OntoMapper.kgen.is_done):
            #        break # Stop condition
            self.mapping_textedit.clear()
            async for chunk in self.ontology_mapper.interact(
                    rationale=self.plan_builder.answer if not from_ontology else "",
                    ontology=self.ontology_builder.answer if from_ontology else "",
                    mapping_extension=self.metadata_manager.mapping_extension,
                    example_extension=self.ontology_mapper.example_extension,
                    ontology_extension=self.metadata_manager.ontology_extension,
                    error=self.RAG_OntoMapper.kgen.error_feedback):
                self.mapping_textedit.insertPlainText(chunk)
                # Scroll to the bottom to ensure the latest message is visible
                self.mapping_textedit.verticalScrollBar().setValue(
                    self.mapping_textedit.verticalScrollBar().maximum()
                )
                QCoreApplication.processEvents()
                if self.isStopped: break
            #    max_iter+=1
            # Reset the variable to generate the mapping again if we want to.
            #if (self.RAG_OntoMapper.kgen is None or self.RAG_OntoMapper.kgen.is_done):
            #    self.RAG_OntoMapper.kgen.reset_internal_state()
            if not self.isStopped:
                self.log.append_log(message="\nINFO: Mappings created succesfully", level="info", end="\n")
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        finally:
            self.isStopped = False
            # Now we reconnect again the listener (if any)
            self.mapping_textedit.textChanged.connect(
                lambda: self.on_text_changed(self.ontology_mapper, self.mapping_textedit)
            )
            # Re-enable the ontology text edit after processing
            self.mapping_textedit.setEnabled(True)

    def create_tripplets(self) -> None:
        """
            Create tripplets from the previously created mappings.
        """
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            # If there's no `data_description`, we missed the "description" step
            # if not self.plan_builder.answer:
            #     raise MissedPreviousStepException(step="HIGH LEVEL STRUCTURE", tab=1)
            # If there's no `data_description`, we missed the "mapping" step
            if not self.ontology_mapper.answer:
                raise MissedPreviousStepException(step="MAPPING", tab=4)
            print('creating tripplets...')
            # We create the tripplets from the mappings
            self.log.append_log(message="\nI will generate the knowledge graph.", level="warning")
            self.RAG_OntoMapper.generateKG()
            # TODO: This log could appear although a error is encountered in the process (i.e. the error is not raised)
            self.log.append_log(
                message="\nKnowledge Graph Generation Status: "
                        + "file 'data.nt' created succesfully", #self.RAG_OntoMapper.kgen.error_feedback,
                level="warning"
            )
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except SyntaxError as se:
            print("Triplets file 'data.nt' couldn't be created:\n",e)
            # An error ocurred during the parsing process and Bad syntax was encountered.
            if issubclass(type(se.msg), BadSyntax):
                error = "".join([
                    "```python\n",
                    str(se).replace("\\n", "\n"), 
                    "\n```",
                    "\n\nThe previous error is produced for the following mappings:\n", 
                    f"```{self.metadata_manager.mapping_extension}\n", # "ttl"
                    se.msg._str.decode(encoding='utf-8', errors='strict'),
                    "\n```"]) #f"Line {se.msg.lines}: {se.msg._why}"
            self.ontology_mapper.error_message = error
            self.log.append_log(message=f"\nERROR: Couldn't parse mappings.", level="error", end="\n")
        except ValueError as ve:
            print("Triplets file 'data.nt' couldn't be created:\n",ve)
            # The column X does not exists in the input data, but is presented in the ontology/mapping step
            if "Usecols do not match columns" in str(ve.args[0]):
                self.getNotUsecols(ve.args[0])
            self.log.append_log(message=f"\nTriplets file 'data.nt' couldn't be created:\n{ve}", level="error")
        except Exception as e:
            print("Triplets file 'data.nt' couldn't be created:\n",e)
            self.log.append_log(message=f"\nTriplets file 'data.nt' couldn't be created:\n{e}", level="error")

    def getNotUsecols(self, value:str):
        aux = value[value.find("[")+1:value.find("]")]
        if len(aux) > 1:
            auxList = aux.replace("'", "").split(",")
            print(auxList)
            # if len(auxList) == 1:
            #     _dataset = pd.read_csv()
            #     _id = find_unique_identifier(_dataset)

    def create_mermaid(self) -> None:
        """Generate a mermaid diagram from the ontology text."""
        if self.current_task is not None and not self.current_task.done():
            self.log.append_log("An OntoGenix action is already in progress.", level="warning", end="\n")
            return
        try:
            # If there's no `json_data`, we missed the "loading csv" step
            if not self.json_data:
                raise MissedPreviousStepException(step="LOAD CSV")
            # If there's no `data_description`, we missed the "description" step
            if not self.plan_builder.answer:
                raise MissedPreviousStepException(step="HIGH LEVEL STRUCTURE", tab=1)
            # If there's no `data_description`, we missed the "mapping" step
            if not self.ontology_builder.answer:
                raise MissedPreviousStepException(step="ONTOLOGY", tab=2)
            # Generate the mermaid diagram
            self.OUTPUT_tab.setCurrentIndex(3)            
            self.log.append_log(message="\nGenerating the mermaid diagram...", level="warning")
            self.mermaid_generator.interact(self.onto_manager.toPlainText())
            graph = self.mermaid_generator.get_diagram()
            context_revision = self._context_revision
            dialog = QDialog(self)
            dialog.setWindowTitle("Confirm external diagram rendering")
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Send this diagram source to mermaid.ink for rendering?"))
            preview = QPlainTextEdit(dialog)
            preview.setReadOnly(True)
            preview.setPlainText(graph or "")
            layout.addWidget(preview)
            buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.Cancel, parent=dialog)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if (dialog.exec_() != QDialog.Accepted or context_revision != self._context_revision
                    or self.isStopped):
                return
            self.mermaid_widget.plot(graph)

            # TODO: check if the mermaid diagram returns a 200 HTTP response code. Or change the library
            if not self.isStopped:
                self.log.append_log(message="\nINFO: Diagram created succesfully", level="info", end="\n")
        except MissedPreviousStepException as mspe:
            message = "\nError: " + mspe.message
            self.log.append_log(message=message, level="error", end="\n")
            self.OUTPUT_tab.setCurrentIndex(mspe.tab)
            # Rollback to the previous step of the automaton
            self.genie.automata.droid.rollback_transition()
            self.log.append_log(message="INFO: Rolling back to previous state", level="warning", end="\n")
        except Exception as e:
            self.log.append_log(f"An error occurred while generating the mermaid diagram: {e}", level="error")
            self.OUTPUT_tab.setCurrentIndex(3)

    @staticmethod
    def move_cursor_to_end(text_edit):
        """Move the text cursor to the end of LLM answer_textedit."""
        cursor = text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        text_edit.setTextCursor(cursor)

    def save_file_dialog(self, title: str, file_filter: str) -> str:
        """Open a save file dialog and return the selected file's path."""
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        file_name, _ = QFileDialog.getSaveFileName(self, title, "", file_filter, options=options)
        return file_name

    def save_candidate_ontology(self) -> None:
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Save candidate ontology", "",
            "Turtle (*.ttl);;RDF/XML (*.rdf *.owl)",
            options=QFileDialog.DontUseNativeDialog,
        )
        if not filename:
            return
        rdf_format = "xml" if selected_filter.startswith("RDF/XML") else "turtle"
        suffix = ".rdf" if rdf_format == "xml" else ".ttl"
        if Path(filename).suffix.lower() not in ({".rdf", ".owl"} if rdf_format == "xml" else {".ttl"}):
            filename += suffix
        try:
            serialized = self.onto_manager.serialize_candidate_text(self.onto_manager.toPlainText(), rdf_format)
            Path(filename).write_text(serialized, encoding="utf-8")
        except Exception as error:
            self.log.append_log(f"Candidate ontology export failed: {error}", level="error")
        else:
            self.log.append_log(f"Saved candidate ontology as {rdf_format}: {filename}")

    def save(self, text: str, extension: str) -> None:
        """Open a file dialog to save the given text with the specified extension."""
        filter_str = f"Text Files (*.{extension})"
        file_name = self.save_file_dialog(title="Save File", file_filter=filter_str)
        if not file_name:  # User cancelled the file dialog
            return
        try:
            file_name = file_name + "." + extension
            with open(file_name, 'w', encoding='utf-8') as file:
                file.write(text)
        except OSError as error_saving:
            self.log.append_log(f"Creation of the file {file_name} failed", level="error")
            print(error_saving)
        else:
            self.log.append_log(f"Successfully created the file {file_name}")

    def open_file_dialog(self, title: str, file_filter: str) -> str:
        """Open a file dialog and return the selected file's path."""
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog

        file_name, _ = QFileDialog.getOpenFileName(self, title, "", file_filter, options=options)
        return file_name

    def attach_file(self):
        # Allows selection of both PNG files and TXT documents
        file_name = self.open_file_dialog("Load File", "PNG Files (*.png);;TXT Files (*.txt)")
        if not file_name:  # User cancelled the file dialog
            return
        else:
            # Detect the file type based on its extension
            if file_name.endswith('.png'):
                # Handle the PNG file
                self.plan_builder.image_file = file_name
                self.log.append_log(f"Image file {file_name} loaded!")
            elif file_name.endswith('.txt'):
                # Handle the TXT file
                self.plan_builder.text_file = file_name
                self.log.append_log(f"Text document {file_name} loaded!")
            else:
                # Optional: Handle unexpected file types or add more types
                self.log.append_log(f"Unsupported file type selected: {file_name}")


    def load_csv_data(self) -> None:
        """Open a file dialog to load a CSV file and display its data."""
        file_name: str = self.open_file_dialog("Load CSV File", "CSV Files (*.csv)")
        # User cancelled the file dialog
        if not file_name:
            return
        try:
            source_path = Path(file_name).resolve(strict=True)
            session_root = Path(os.environ.get("ONTOGENIX_SESSION_DIR", os.getcwd())).resolve()
            session_root.mkdir(parents=True, exist_ok=True)
            session_dir = session_root / f"session-{uuid.uuid4().hex}"
            session_dir.mkdir(mode=0o700)
            working_copy = session_dir / source_path.name
            shutil.copy2(source_path, working_copy)
            os.chmod(working_copy, 0o600)
            self.metadata_manager.session_directory = str(session_dir)
            self.log.append_log(f"Working copy created in {session_dir}; the selected source remains unchanged.")

            # Set paths and configurations for the metadata manager using the private working copy.
            self.metadata_manager.create_config4morph(str(working_copy))

            # Update the dataset paths for various components
            self.plan_builder.dataset_path = self.metadata_manager.dataset_child_folder_path
            self.ontology_builder.dataset_path = self.metadata_manager.dataset_child_folder_path
            self.ontology_mapper.dataset_path = self.metadata_manager.dataset_full_path
            self.RAG_OntoMapper.build_kgen(self.metadata_manager.dataset_metadata)
            # Convert the CSV data to JSON and display in GUI
            dataframe = csv_data_preprocessing(self.metadata_manager.dataset_full_path)
            self.json_data = dataframe2prettyjson(dataframe)
            self.csv_textedit.setText(self.json_data)

            # Enable button for saving
            self.save_json_btn.setEnabled(True)
            # Update the output tab and log the success
            self.log.append_log(f"{file_name} loaded!")
        except (OSError, ValueError) as e:
            self.log.append_log(f"An error occurred while loading csv data: {e}", level="error")
        finally:
            self.OUTPUT_tab.setCurrentIndex(0)
