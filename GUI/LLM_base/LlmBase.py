"""
    Base class for the support of the different OntoGenix modules.
    TODO: Document this
"""

import asyncio
import base64
import json
import os
from abc import ABC

from openai import AzureOpenAI, OpenAI
from typing import Union

class AbstractLlm(ABC):
    error_message = None

    def __init__(self, metadata: dict):
        self.client_type = metadata["client"]
        self.api_model = metadata["model"]
        self.confirm_external_request = metadata.get("confirm_external_request")
        self.confirm_generated_action = metadata.get("confirm_generated_action")
        self.scoped_authorization = metadata.get("scoped_authorization")
        self.api_version = os.environ.get("OPENAI_API_VERSION")
        self.azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        self.azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        self.base_url = os.environ.get("OPENAI_BASE_URL")
        self.client = None
        # set client properties
        self.role = metadata['role']
        self.model = self.azure_deployment or self.api_model
        self.answer = ""
        #self.error_message = None
        self.tool_calls = None
        self.available_functions = None
        # set utilities
        self.last_prompt = None
        self.current_prompt = None
        try:
            # NOTE: https://cookbook.openai.com/examples/reproducible_outputs_with_the_seed_parameter
            # NOTE: https://community.openai.com/t/is-the-seed-parameter-being-deprecated/578586/5
            self.seed = int(metadata['seed'])
        except:
            self.seed = None

    def _get_client(self):
        if self.client is not None:
            return self.client
        if self.client_type == "azure":
            if not self.azure_endpoint or not self.api_version:
                raise ValueError("Azure client requires AZURE_OPENAI_ENDPOINT and OPENAI_API_VERSION.")
            api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("Azure client requires AZURE_OPENAI_API_KEY (or OPENAI_API_KEY).")
            self.client = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=self.azure_endpoint,
                api_version=self.api_version,
                azure_deployment=self.azure_deployment or self.api_model,
                **({"max_retries": 0} if self.scoped_authorization is not None else {}),
            )
        else:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("Set OPENAI_API_KEY in the dedicated OntoGenix configuration before making a model request.")
            self.client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                **({"max_retries": 0} if self.scoped_authorization is not None else {}),
            )
        return self.client

    def _confirm_external_request(self, messages, purpose="model request", max_tokens=None):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self.scoped_authorization is not None:
            return self.scoped_authorization.authorize(messages, purpose, self.model, max_tokens)
        if self.confirm_external_request is None:
            raise PermissionError("No human confirmation handler is configured; external disclosure was blocked.")
        endpoint = self.azure_endpoint if self.client_type == "azure" else self.base_url
        if not self.confirm_external_request(self.api_model, endpoint, messages, purpose):
            raise PermissionError("The user declined external disclosure; no request was sent.")
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        return {}

    def _record_scoped_completion(self, completion):
        if self.scoped_authorization is not None:
            self.scoped_authorization.record_completion(completion)

    def get_api_response(self, content: str, temperature:int=0, max_tokens:Union[int|None]=None,
                         #stream:Union[bool|None]=False, 
                         seed:Union[int|None]=None):
        messages = [{"role": "system", "content": self.role}, {"role": "user", "content": content}]
        request_options = self._confirm_external_request(messages, max_tokens=max_tokens)
        max_tokens = request_options.pop("max_tokens", max_tokens)
        completion = self._get_client().chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **request_options,
        )
        self._record_scoped_completion(completion)

        try:
            self.answer = completion.choices[0].message.content
            return self.answer
        except:
            #print(">>> empty chunk response")
            return ""


    async def get_async_api_response(self, content: str,temperature:int=0, max_tokens:Union[int|None]=None,
                         #stream:Union[bool|None]=True, 
                         seed:Union[int|None]=None):
        self.answer = ""
        messages = [{"role": "system", "content": self.role}, {"role": "user", "content": content}]
        self._confirm_external_request(messages)
        completion = self._get_client().chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
        )

        for chunk in completion:
            try:
                data = chunk.choices[0].delta.content
                if data is not None:
                    self.answer += data
                    yield data
            except:
                #print(">>> empty chunk response")
                yield ""

    async def image_interpretation(self, content: str, image_file: str, seed:Union[int|None]=None):
        base64_image, image_extension = self.image2base64(image_file)

        messages = [{"role": "system", "content": self.role}, {
            "role": "user",
            "content": [
                {"type": "text", "text": content},
                {"type": "image_url", "image_url": f"data:image/{image_extension};base64,{base64_image}"},
            ],
        }]
        self._confirm_external_request(messages, purpose=f"image request ({image_file})")
        completion = self._get_client().chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0,
            max_tokens=1024,
            stream=True,
        )

        for chunk in completion:
            data = chunk.choices[0].delta.content
            if data is not None:
                self.answer += data
                yield data

    def function_calling(self, content: str, tools=Union[list|None], seed:Union[int|None]=None):
        # TODO: In Azure, for some reason, the "ontology_entity_enrichment" tool_call is None when trying to enrich the ontology
        messages = [{"role": "system", "content": self.role}, {"role": "user", "content": content}]
        self._confirm_external_request(messages, purpose="tool-selection model request")
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
        )

        self.tool_calls = response.choices[0].message.tool_calls

    async def regenerate(self):
        try:
            # Reuse the last prompt
            async for chunk in self.get_async_api_response(self.current_prompt):
                yield chunk
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            return
        except ValueError as e:
            print(f"An error occurred while extracting text: {e}")
        finally:
            self.last_prompt = self.current_prompt

    async def continue_writing(self):
        try:
            continue_prompt = '''You did not finish your writing during your last answer.

                                        This is your last answer:
                                        {answer}

                                        Complete the previous answer and continue from where you stopped.'''
            continue_prompt.format(answer=self.answer)
            # Reuse the last prompt
            async for chunk in self.get_async_api_response(continue_prompt):
                yield chunk
        except GeneratorExit as ge:
            print(f"Process stopped/cancelled by user: {ge}")
            return
        except ValueError as e:
            print(f"An error occurred while extracting text: {e}")

    async def _process_function_response(self):
        """Processes the response message from model and calls the intended function.
        :param function_callback: callback function which be called with the corresponding arguments.
        :return: the function to be returned
        """
        if not self.tool_calls:
            raise ValueError("No tool_call available for the current action.")
        elif not self.available_functions:
            raise ValueError("No available_function available for the current tool_call.")
        
        for tool_call in self.tool_calls:
            function_name = tool_call.function.name
            function_to_call = self.available_functions.get(function_name)
            if function_to_call is None:
                print(f"Function {function_name} not found in metadata.")
                continue

            try:
                function_args = json.loads(tool_call.function.arguments)
                if not isinstance(function_args, dict):
                    raise ValueError("Generated action arguments must be an object.")
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    return
                if self.confirm_generated_action is None or not self.confirm_generated_action(function_name, function_args):
                    continue
                if task is not None and task.cancelling():
                    return
                await function_to_call(**function_args)
            except json.JSONDecodeError as error:
                print(f"Error decoding JSON: {error}")
            except Exception as error:
                print(f"Error during function call: {error}")

    @staticmethod
    def image2base64(image_file):
        image_extension = image_file.split('.')[-1]
        # Convert image to base64
        with open(image_file, "rb") as image:
            base64_image = base64.b64encode(image.read()).decode('utf-8')

        return base64_image, image_extension

    @staticmethod
    def load_string_from_file(file_path, encoding:str="utf-8"):
        try:
            with open(file_path, 'r', encoding=encoding) as file:
                return file.read()
        except Exception as e:
            print(e)
        return None
    

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
        if start_index == (len(start_marker) - 1):
            raise ValueError("Start marker not found in text.")
        elif end_index == -1:
            raise ValueError("End marker not found in text.")
        return text[start_index:end_index].strip()

    @staticmethod
    def save_response(response: str, file: str, mode: str = 'w'):
        try:
            with open(file, mode) as f:
                f.write(response + '\n')
        except ValueError as e:
            print(f"An error occurred: {e}")
