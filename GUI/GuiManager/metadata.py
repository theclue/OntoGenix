"""
    File that contains the metadata for the LLM client (just chatGPT at the moment)

    TODO: Maybe use some hierarchical structure for the different LLMs involved
"""
import configparser
import os

from typing import Union

from GUI.tools import filepath_tools
from GUI.tools.constants import OS_SEPARATOR, ACK_MAPPING_FORMAT, ACK_ONTOLOGY_FORMAT # pylint: disable = no-name-in-module

class DatasetMetadata:
    """
        Auxiliary class for storing the dataset important metadata

        TODO: separate class into different file
    """
    # Path
    base_path : str = None
    dataset_folder : str = None
    dataset_file : str = None
    # File
    dataset_name : str = None
    dataset_ext : str = None
    # Other
    encoding : str = "utf-8"
    mime_type : str = "text/plain"
    size : int = -1 # No file read
    # Ontogenix
    _ontology_extension: str = "ttl"
    _mapping_extension: str = "rml"

    def __init__(self, file_path : str, ontology_extension:str = "ttl", mapping_extension:str = "rml"):
        """
            Initialize the variables

            TODO: check if file exists and handle errors
        """
        # Initializing the dataset metadata values
        self.base_path, self.dataset_folder, self.dataset_file = filepath_tools.parse_dataset_path(file_path)
        self.dataset_name, self.dataset_ext = os.path.splitext(self.dataset_file)
        self.encoding, self.mime_type = filepath_tools.check_file_encoding_mime_type(file_path)
        self.size : int = os.path.getsize(file_path)
        # Initializating the ontogenix files metadata
        if ontology_extension is not None and ontology_extension.lower().strip() in ACK_ONTOLOGY_FORMAT:
            self._ontology_extension = ontology_extension.lower().strip()
        if mapping_extension is not None and mapping_extension.lower().strip() in ACK_MAPPING_FORMAT:
            self._mapping_extension = mapping_extension.lower().strip()

    @property
    def dataset_base_dir(self) -> str:
        """
            Returns the full path of the base folder of the dataset
        """
        #return self.join_paths(self.base_path, self.dataset_folder, '')
        return OS_SEPARATOR.join([path for path in [self.base_path, self.dataset_folder, ""] if path is not None and len(path) > 0])

    @property
    def dataset_full_path(self) -> str:
        """
            Returns the full path of the dataset
        """
        #return self.join_paths(self.base_path, self.dataset_folder, f'{self.dataset_file}.csv')
        return OS_SEPARATOR.join([path for path in [self.base_path, self.dataset_folder, self.dataset_file] if path is not None and len(path) > 0])

    @property
    def dataset_child_folder_path(self) -> str:
        """
            Returns a the full path of the dataset child folder
        """
        #return self.join_paths(self.base_path, self.dataset_folder, self.dataset_file+'/')
        return OS_SEPARATOR.join([path for path in [self.base_path, self.dataset_folder, self.dataset_name, ""] if path is not None and len(path) > 0])
    
    @property
    def dataset_config_path(self) -> str:
        """
            Returns a the full path of the dataset config's file
        """
        return self.new_file_full_path('config.ini')
    
    @property
    def dataset_triplets_path(self) -> str:
        """
            Returns a the full path of the dataset triplet's file
        """
        return self.new_file_full_path('data.nt')
    
    @property
    def dataset_mapping_path(self):
        """
            Checks the accepted formats for mapping and returns by default the TTL format if any problem is encountered
        """
        return self.new_file_full_path("_".join([self.dataset_name, "mapping_LLM."+self._mapping_extension]))

    def new_file_full_path(self, file : str) -> str: # noqa
        """
            Returns the full path of the new file, from the base path of the dataset metadata

            Args:
                file: The name of the new file (with its extension)

            # TODO: check file name size for compliance with the convention (Windows' maximum file name size is 128, I think)
            # TODO: check if the file has a valid name (without not valid characters)
            # TODO: move this funtionality to the path_tools.py file
        """
        new_path = OS_SEPARATOR.join([path for path in [self.base_path, self.dataset_folder, file] if path is not None and len(path) > 0])
        if not filepath_tools.is_safe_path(self.dataset_base_dir, new_path):
            raise ValueError("Path travesal is not allowed")
        return new_path
    
    def as_dict(self):
        """
            Returns a dictionary of the dataset metadata

            # TODO: not fully implemented
        """
        return {
            "full_path": self.dataset_full_path,
            "base_path": self.base_path,
            "dataset_folder": self.dataset_folder,
            "dataset_file": self.dataset_file,
            "dataset_name": self.dataset_name,
            "dataset_ext": self.dataset_ext,
            "encoding": self.encoding,
            "mime_type": self.mime_type,
            "size": self.size,
            "dataset_base_dir": self.dataset_base_dir,
            "dataset_full_path": self.dataset_full_path,
            "dataset_child_folder_path": self.dataset_child_folder_path,
            "dataset_config_path": self.dataset_config_path,
            "dataset_triplets_path": self.dataset_triplets_path,
            "dataset_mapping_path": self.dataset_mapping_path
        }

    # TODO: maybe create this later for the API
    # https://www.geeksforgeeks.org/how-to-convert-python-dictionary-to-json/
    #def as_json(self, indent : int = 4):
    #    """
    #        Returns a JSON representation of the dataset metadata
    #    """
    #    return json.dumps(self.as_dict(), indent=indent)

class MetadataManager:
    """
        This class manages metadata for different components of the application.
        TODO: Basically, this class can be read from a configuration file (JSON).
        TODO: Parameter for choosing the LLM and api model
        NOTE: The seed parameter is avaliable only for the first two models:
            https://cookbook.openai.com/examples/reproducible_outputs_with_the_seed_parameter
        NOTE: check top_p parameter
    """
    dataset_metadata : DatasetMetadata = None
    # TODO: Read the following parameters from a configuration file (for example, ontogenix_config.json)
    base_path : str = "./GUI"
    #client : str = "openai" # Parameter for OntoGenix's client
    #ssl_cert : str = None
    seed : Union[int|None] = 123
    
    def __init__(self, *argv: tuple):
        # Just in case, we use the default values
        self.ontology_extension = "ttl" # "RDF/XML".lower().strip()
        self.mapping_extension = "yarrrml" # "ttl"
        self.ssl_cert : str = None
        self.client : str = "openai" # Parameter for OntoGenix's client
        configured_model = os.environ.get("ONTOGENIX_API_MODEL")
        if configured_model is None:
            configured_model = os.environ.get("OPENAI_MODEL")
        self.api_model : str = "default" if configured_model is None else configured_model
        if not self.api_model.strip():
            raise ValueError("Model alias configuration must not be empty.")
        # Read arguments from command line
        # TODO: change this to other method
        if argv:
            # [client] Select the API client to use
            if str(argv[0].client).lower().strip() == "azure":
                self.client = "azure"
            # [api_model] Select api_model via parameter to use
            if argv[0].api_model is not None:
                if not argv[0].api_model.strip():
                    raise ValueError("--api-model requires a non-empty model alias.")
                self.api_model = argv[0].api_model
            # [mapping_extension] Select the extension of mapping to use
            if str(argv[0].mapping_extension).lower().strip() in ACK_MAPPING_FORMAT:
                self.mapping_extension = argv[0].mapping_extension
            # TODO: do something
            # if ontology_extension is not None and ontology_extension.lower().strip() in ACK_ONTOLOGY_FORMAT:
            #     self._ontology_extension = ontology_extension.lower().strip()
            # [ssl_cert] NOTE: The following certificate is required only when using the BASF instance
            if argv[0].ssl_cert is not None and (os.path.exists(os.path.join(os.getcwd(), argv[0].ssl_cert))
                                            or os.path.exists(os.path.join(argv[0].ssl_cert))):
                cacert = filepath_tools.path2posix(argv[0].ssl_cert)
                os.environ["REQUESTS_CA_BUNDLE"] = cacert
                os.environ["SSL_CERT_FILE"] = cacert
                self.ssl_cert = cacert
            elif self.client == "azure" and self.ssl_cert is None:
                cert_path = os.environ.get("CERT_PATH")
                if cert_path and os.path.exists(cert_path):
                    cacert = filepath_tools.path2posix(cert_path)
                    os.environ["REQUESTS_CA_BUNDLE"] = cacert
                    os.environ["SSL_CERT_FILE"] = cacert
                    self.ssl_cert = cacert

        # Interpolation of data into the JSON metadata template
        gui_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.base_path = gui_root
        configuration = filepath_tools.interpolate_json(
            json_path = os.path.join(gui_root, 'GuiManager/metadata.json'),
            data = {
                "base_path" : self.base_path,
                "api_model": self.api_model,
                "client": self.client,
                "ssl_cert": self.ssl_cert,
                "seed": self.seed,
                "ontology_extension": self.ontology_extension,
                "mapping_extension": self.mapping_extension,
                "confirm_external_request": getattr(self, "confirm_external_request", None),
                "confirm_generated_action": getattr(self, "confirm_generated_action", None),
                "confirm_external_search": getattr(self, "confirm_external_search", None),
            },
            encoding = 'utf-8'
        )
        tools_definition = filepath_tools.interpolate_json(
            json_path = os.path.join(gui_root, 'GuiManager/tools_definition.json'),
            data = {
                "ontology_extension": self.ontology_extension,
                "mapping_extension": self.mapping_extension,
            },
            encoding = 'utf-8'
        )
        # Prepare metadata with additional arguments. TODO: are these 2 parameters really necessary?
        self.genie_metadata = configuration.get('genie_metadata') | {
            "tools": tools_definition.get("tools"),
            "available_functions": tools_definition.get("available_functions")
        }
        self.crafter_metadata = configuration.get('crafter_metadata')
        self.planner_metadata = configuration.get('planner_metadata')
        self.onto_metadata = configuration.get('onto_metadata')
        self.mapper_metadata = configuration.get('mapper_metadata')
        self.mermaid_metadata = configuration.get('mermaid_metadata')

    @property
    def dataset_full_path(self) -> str:
        """
            Returns the full path of the dataset
        """
        if self.dataset_metadata is None:
            return None
        return self.dataset_metadata.dataset_full_path

    @property
    def dataset_child_folder_path(self) -> str:
        """
            Returns a the full path of the dataset child folder
        """
        if self.dataset_metadata is None:
            return None
        return self.dataset_metadata.dataset_child_folder_path

    def create_config4morph(self, given_path: str):
        """
            Method that initializes the metadata for a dataset in a given path. Also, it
            creates the 'config.ini' file for the later graph generation process.

            TODO: improve file management (separate in other function)
            TODO: Unnecessary to create config.ini file if it can be passed to morph-kgc.materialize() directly

            Args:
                given_path: absolute path of the dataset
        """
        # Initialize the dataset metadata
        self.dataset_metadata = DatasetMetadata(given_path, self.ontology_extension, self.mapping_extension)
        # Create the config file for the output mapping
        config = configparser.ConfigParser()
        mappings_path = self.dataset_metadata.dataset_mapping_path
        config['DataSourceCSV'] = {
            'mappings': mappings_path,
            'file_path': self.dataset_full_path
        }   
        config_path = self.dataset_metadata.dataset_config_path
        # TODO: check if there's actually permission to create/modify a file or it's a read-only directory. If not, it might raise OSError
        with open(config_path, 'w', encoding='utf-8') as configfile:
            config.write(configfile)
