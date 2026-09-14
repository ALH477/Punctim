# Copyright (C) 2025 DeMoD LLC
#
# This file is part of the Punctim LLM Interface System.
#
# The Punctim LLM Interface System is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# The Punctim LLM Interface System is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

import asyncio
import os
import ctypes
import hashlib
from cryptography.fernet import Fernet, InvalidToken
import aiohttp
import grpc
import dcf_pb2 as pb  # From compiled dcf.proto
import dcf_pb2_grpc as pb_grpc
import json
import re
import logging
import time

logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

# JSONC Parser
def strip_jsonc_comments(text, allow_trailing_commas=True):
    result = []
    i = 0
    in_string = False
    in_single_comment = False
    in_multi_comment = False
    length = len(text)
    while i < length:
        char = text[i]
        if in_single_comment:
            if char == '\n':
                in_single_comment = False
            i += 1
            continue
        if in_multi_comment:
            if char == '*' and i + 1 < length and text[i + 1] == '/':
                in_multi_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            result.append(char)
            if char == '\\':
                i += 1
                if i < length:
                    result.append(text[i])
                    i += 1
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue
        if char == '/' and i + 1 < length:
            next_char = text[i + 1]
            if next_char == '/':
                in_single_comment = True
                i += 2
                continue
            if next_char == '*':
                in_multi_comment = True
                i += 2
                continue
        result.append(char)
        i += 1
    if in_string:
        raise ValueError("Unbalanced string in JSONC")
    if in_multi_comment:
        raise ValueError("Unbalanced multi-line comment in JSONC")
    cleaned = ''.join(result).strip()
    if allow_trailing_commas:
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return cleaned

def parse_jsonc(data_str, allow_trailing_commas=True):
    try:
        cleaned = strip_jsonc_comments(data_str, allow_trailing_commas)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON decode error: {str(e)}")
    except ValueError as e:
        raise ValueError(f"JSONC parsing error: {str(e)}")

# Config Schema Validator
def validate_config(instance, schema):
    def validate_value(value, prop_schema):
        val_type = type(value).__name__.lower()
        if prop_schema.get('type') != val_type:
            return False
        if 'enum' in prop_schema and value not in prop_schema['enum']:
            return False
        if 'minimum' in prop_schema and value < prop_schema['minimum']:
            return False
        if 'maximum' in prop_schema and value > prop_schema['maximum']:
            return False
        if prop_schema.get('type') == 'array':
            item_schema = prop_schema.get('items', {})
            for item in value:
                if not validate_value(item, item_schema):
                    return False
        if prop_schema.get('type') == 'object':
            for k, v in value.items():
                sub_schema = prop_schema.get('properties', {}).get(k, {})
                if not validate_value(v, sub_schema):
                    return False
        return True

    if not validate_value(instance, schema):
        return False
    required = schema.get('required', [])
    for req in required:
        if req not in instance:
            return False
    if not schema.get('additionalProperties', True):
        allowed_keys = set(schema.get('properties', {}).keys())
        if set(instance.keys()) - allowed_keys:
            return False
    return True

SCHEMA = {
  "type": "object",
  "properties": {
    "transport": {"type": "string", "enum": ["gRPC", "native-lisp", "WebSocket"]},
    "host": {"type": "string"},
    "port": {"type": "integer", "minimum": 0, "maximum": 65535},
    "mode": {"type": "string", "enum": ["client", "server", "p2p", "auto", "master"]},
    "node-id": {"type": "string"},
    "peers": {"type": "array", "items": {"type": "string"}},
    "group-rtt-threshold": {"type": "integer", "minimum": 0, "maximum": 1000},
    "plugins": {"type": "object"}
  },
  "required": ["transport", "host", "port", "mode"],
  "additionalProperties": False
}

def load_hydra_config(config_path='config.jsonc'):
    try:
        with open(config_path, 'r') as f:
            config_str = f.read()
        config = parse_jsonc(config_str)
        if not validate_config(config, SCHEMA):
            raise ValueError("Configuration does not match schema")
        return config
    except FileNotFoundError:
        raise ValueError(f"Config file not found: {config_path}")
    except IOError as e:
        raise ValueError(f"IO error reading config: {str(e)}")
    except ValueError as e:
        raise ValueError(f"Config validation error: {str(e)}")

# StreamDB Wrapper
class StreamDB:
    def __init__(self, lib_path="libstreamdb.so", db_path="dcf.streamdb", quick_mode=True):
        try:
            self.lib = ctypes.CDLL(lib_path)
            self.lib.streamdb_open_with_config.restype = ctypes.c_void_p
            self.lib.streamdb_open_with_config.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            self.lib.streamdb_write_document.restype = ctypes.c_char_p
            self.lib.streamdb_write_document.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
            self.lib.streamdb_get.restype = ctypes.c_void_p
            self.lib.streamdb_get.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)]
            self.lib.streamdb_delete.restype = ctypes.c_int
            self.lib.streamdb_delete.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            self.lib.streamdb_search.restype = ctypes.c_void_p
            self.lib.streamdb_search.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
            self.lib.streamdb_flush.restype = ctypes.c_int
            self.lib.streamdb_flush.argtypes = [ctypes.c_void_p]
            self.lib.streamdb_close.restype = None
            self.lib.streamdb_close.argtypes = [ctypes.c_void_p]
            self.lib.streamdb_set_quick_mode.restype = None
            self.lib.streamdb_set_quick_mode.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            self.db = self.lib.streamdb_open_with_config(db_path.encode('utf-8'), None)
            if not self.db:
                raise ValueError("Failed to open StreamDB")
            if quick_mode:
                self.lib.streamdb_set_quick_mode(self.db, True)
        except OSError as e:
            raise ValueError(f"Failed to load libstreamdb.so: {str(e)}")
        except Exception as e:
            raise ValueError(f"StreamDB init error: {str(e)}")

    def insert(self, path, data):
        try:
            data_bytes = data.encode('utf-8')
            data_ptr = ctypes.cast(data_bytes, ctypes.c_void_p)
            guid = self.lib.streamdb_write_document(self.db, path.encode('utf-8'), data_ptr, len(data_bytes))
            if not guid:
                raise ValueError("StreamDB insert failed")
            return guid.decode('utf-8')
        except Exception as e:
            raise ValueError(f"StreamDB insert error: {str(e)}")

    def get(self, path):
        try:
            size = ctypes.c_size_t()
            data_ptr = self.lib.streamdb_get(self.db, path.encode('utf-8'), ctypes.byref(size))
            if not data_ptr:
                return None
            data = ctypes.string_at(data_ptr, size.value).decode('utf-8')
            ctypes.free(data_ptr)
            return data
        except Exception as e:
            raise ValueError(f"StreamDB get error: {str(e)}")

    def delete(self, path):
        try:
            if self.lib.streamdb_delete(self.db, path.encode('utf-8')) != 0:
                raise ValueError("StreamDB delete failed")
        except Exception as e:
            raise ValueError(f"StreamDB delete error: {str(e)}")

    def search(self, prefix):
        try:
            count = ctypes.c_int()
            results_ptr = self.lib.streamdb_search(self.db, prefix.encode('utf-8'), ctypes.byref(count))
            if not results_ptr:
                return []
            results = []
            ptr_array = ctypes.cast(results_ptr, ctypes.POINTER(ctypes.c_char_p))
            for i in range(count.value):
                results.append(ptr_array[i].decode('utf-8'))
            ctypes.free(results_ptr)
            return results
        except Exception as e:
            raise ValueError(f"StreamDB search error: {str(e)}")

    def flush(self):
        try:
            if self.lib.streamdb_flush(self.db) != 0:
                raise ValueError("StreamDB flush failed")
        except Exception as e:
            raise ValueError(f"StreamDB flush error: {str(e)}")

    def close(self):
        try:
            if self.db:
                self.lib.streamdb_close(self.db)
                self.db = None
        except Exception as e:
            logging.error(f"StreamDB close error: {str(e)}")

# LLM API
async def call_llm_api_async(prompt, api_key, model="grok-beta", retries=3, backoff=1):
    url = "https://api.x.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True}
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(response.request_info, response.history, status=response.status)
                    content = ""
                    async for line in response.content:
                        if line:
                            chunk = line.decode('utf-8').strip()
                            if chunk.startswith("data: "):
                                chunk_data = json.loads(chunk[6:])
                                if 'choices' in chunk_data:
                                    delta = chunk_data['choices'][0]['delta'].get('content', '')
                                    content += delta
                                    yield delta
                    yield content
                    return
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise ValueError(f"LLM API error after {retries} retries: {str(e)}")
            await asyncio.sleep(backoff * (2 ** attempt))

# Main Interface
async def interface_llm_with_hydra(prompt, api_key, config, db_path="dcf.streamdb", encryption_key=None, retries=3):
    db = None
    try:
        db = StreamDB(db_path=db_path)
        grpc_address = f"{config['host']}:{config['port']}"
        model = config.get('model', 'grok-beta')

        llm_content = ""
        async for chunk in call_llm_api_async(prompt, api_key, model, retries):
            if isinstance(chunk, str) and chunk:
                llm_content += chunk

        message = pb.DCFMessage(
            sender=config.get('node-id', 'python-client'),
            recipient="hydra-node",
            text_data=llm_content,
            timestamp=int(asyncio.get_event_loop().time()),
            sync=True,
            sequence=int.from_bytes(hashlib.sha256(llm_content.encode()).digest()[0:4], 'big'),
            redundancy_path="",
            group_id=""
        )
        serialized = message.SerializeToString()
        if encryption_key:
            try:
                fernet = Fernet(encryption_key)
                serialized = fernet.encrypt(serialized)
            except InvalidToken:
                raise ValueError("Invalid encryption key")

        for attempt in range(retries):
            try:
                async with grpc.aio.insecure_channel(grpc_address) as channel:
                    stub = pb_grpc.DCFServiceStub(channel)
                    await stub.SendMessage(message)
                    break
            except grpc.RpcError as e:
                if attempt == retries - 1:
                    raise ValueError(f"gRPC error after {retries} retries: {str(e)}")
                await asyncio.sleep(1 * (2 ** attempt))

        guid = db.insert("/llm/responses", llm_content)
        db.flush()

    except ValueError as e:
        raise e
    except Exception as e:
        raise ValueError(f"Unexpected error: {str(e)}")
    finally:
        if db:
            db.close()

async def main():
    try:
        config_path = os.getenv('HYDRA_CONFIG', 'config.jsonc')
        config = load_hydra_config(config_path)
        api_key = os.getenv('GROK_API_KEY')
        if not api_key:
            raise ValueError("GROK_API_KEY environment variable not set")
        encryption_key = os.getenv('ENCRYPTION_KEY')
        prompt_str = os.getenv('PROMPT_JSONC', '{"prompt": "Explain quantum computing."}')
        prompt_data = parse_jsonc(prompt_str)
        prompt = prompt_data.get('prompt')
        if not prompt:
            raise ValueError("No prompt provided in PROMPT_JSONC")
        await interface_llm_with_hydra(prompt, api_key, config, encryption_key=encryption_key)
    except ValueError as e:
        logging.error(str(e))
        raise

if __name__ == "__main__":
    asyncio.run(main())
