"""
    Constants file
"""
import os

OS_SEPARATOR:str = os.path.altsep if (os.path.altsep is not None) else os.path.sep # "/"
ACK_MAPPING_FORMAT:set[str] = frozenset(["ttl", "yarrrml"]) # "yml", "yaml", 
ACK_ONTOLOGY_FORMAT:set[str] = frozenset(["ttl"])
