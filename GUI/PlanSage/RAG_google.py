from typing import Dict, List, Optional
#from serpapi import GoogleSearch # https://github.com/serpapi/google-search-results-python/issues/63
import os
import serpapi
import difflib
import re


class Searcher:
    """
    A class to search for entities on schema.org using SerpApi's Google Search.
    """

    def __init__(self, metadata) -> None:
        """
        Initialize the searcher with an API key.
        """
        self.enabled = os.environ.get("ONTOGENIX_ENABLE_WEB_SEARCH", "").strip().lower() in {"1", "true", "yes"}
        self.api_key = os.environ.get("SERPAPI_API_KEY") if self.enabled else None
        self.confirm_external_search = metadata.get("confirm_external_search")

    def _search_schema_org(self, query: str, domain: str= 'schema.org') -> Dict[str, str]:
        """
        Search for a given query on schema.org and return entities and their snippets.

        :param query: The search query.
        :return: A dictionary of entities and their corresponding snippets.
        """
        if not self.enabled:
            return {}
        if not self.api_key:
            raise ValueError("Web search is enabled but SERPAPI_API_KEY is not configured.")
        search_query = query + ' ' + domain
        if self.confirm_external_search is None or not self.confirm_external_search(search_query):
            return {}
        try:
            results = serpapi.Client(api_key=self.api_key).search({"engine": "google", "q": search_query})
            organic_results = results.get("organic_results", [])

            entities = {}
            for item in organic_results:
                if item.get('source') == 'Schema.org':
                    key = item['link'].split('/')[-1]

                    if 'snippet' in item.keys():
                        entities[key] = item['snippet']
                    elif 'rich_snippet_table' in item.keys():
                        entities[key] = item['rich_snippet_table']

            return entities
        except Exception:
            print("Web search failed; no result was returned.")
            return {}

    @staticmethod
    def _find_similar_entities(entity: str, entities: List[str], n: int = 5) -> List[str]:
        """
        Find the most similar entities to the input_entity in the list of entities.

        Args:
        input_entity (str): The entity name to search for.
        entities (list): The list of entities to search within.
        n (int): Number of closest matches to return.

        Returns:
        list: A list of the closest matches.
        """
        return difflib.get_close_matches(entity, entities, n=n)

    @staticmethod
    def _exist_entity_schemaorg(entity: str) -> bool:
        """
        Returns True if the entity exists in schema.org, False otherwise.
        """
        url = f"https://schema.org/{entity}"
        response = requests.get(url)

        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'lxml')
            # Look for specific indicators on the page that signify the entity exists
            if soup.find('h1', string=entity):
                return True
            else:
                return False
        else:
            return False
    @staticmethod
    def _extract_items(input_text):
        # Split the input text into lines
        lines = input_text.split("\n")

        # Regular expressions for different formats
        regex_simple = r"^\d+\.\s*(?:\w+:)?(\w+)$"
        regex_subclass = r"^\d+\.\s*(?:\w+:)?(\w+): subclass of"
        regex_domain_range = r"^\d+\.\s*(?:\w+:)?(\w+): domain"

        # List to store extracted entities
        entities = []

        for line in lines:
            # Check and extract based on format
            simple_match = re.match(regex_simple, line)
            subclass_match = re.match(regex_subclass, line)
            domain_range_match = re.match(regex_domain_range, line)

            if simple_match:
                entities.append(simple_match.group(1))
            elif subclass_match:
                entities.append(subclass_match.group(1))
            elif domain_range_match:
                entities.append(domain_range_match.group(1))

        return entities

    @staticmethod
    def extract_text(text: str, start_marker: str, end_marker: str) -> str:
        """
        Extracts a substring of text between two markers and returns it.

        Args:
            text (str): The text to be searched.
            start_marker (str): The start marker of the substring.
            end_marker (str): The end marker of the substring.

        Returns:
            The substring of text between start_marker and end_marker.

        Raises:
            ValueError: If start_marker or end_marker is not found in text.
        """
        start_index = text.find(start_marker) + len(start_marker)
        end_index = text.find(end_marker, start_index)
        if start_index == -1:
            raise ValueError("Start marker not found in text.")
        elif end_index == -1:
            raise ValueError("End marker not found in text.")
        return text[start_index:end_index].strip()

