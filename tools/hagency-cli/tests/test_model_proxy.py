from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hagency_cli.model_proxy import (
    ModelProxyConfigError,
    ProviderAdapter,
    ProviderAdapterError,
    load_proxy_config,
)
from hagency_cli.model_proxy.conversion import (
    ChatToResponsesStream,
    ConversionError,
    ConversionWarnings,
    ResponsesToChatStream,
    convert_request,
    convert_response,
)
from hagency_cli.model_proxy.providers import (
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
)
from hagency_cli.model_proxy.server import create_model_proxy_app
from hagency_cli.model_proxy.sse import SseDecoder, SseEvent, encode_event, json_event


class ModelProxyConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, content: str) -> Path:
        path = self.root / "hagency-model-proxy.toml"
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        return path

    def test_loads_provider_level_config_and_resolves_environment_values(self) -> None:
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"

            [providers.corp]
            adapter = "openai_compatible"
            protocol = "openai_chat_completions"
            base_url = "https://llm.example.invalid/openai/v1/"
            api_key = { env = "CORP_TOKEN" }
            forward_credential_headers = ["Authorization"]

            [providers.corp.headers]
            "X-Literal" = "yes"

            [providers.corp.query]
            "api-version" = "2026-01-01"
            """
        )

        config = load_proxy_config(path, environ={"CORP_TOKEN": "secret"})

        provider = config.providers["corp"]
        self.assertEqual(provider.adapter.name, "openai_compatible")
        self.assertEqual(provider.protocol, PROTOCOL_CHAT)
        self.assertEqual(provider.base_url, "https://llm.example.invalid/openai/v1")
        self.assertEqual(dict(provider.headers)["Authorization"], "Bearer secret")
        self.assertEqual(dict(provider.query)["api-version"], "2026-01-01")
        self.assertEqual(
            provider.forward_credential_headers, frozenset({"authorization"})
        )

    def test_loads_workspace_dotenv_with_process_environment_precedence(self) -> None:
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"

            [providers.corp]
            adapter = "openai_compatible"
            protocol = "openai_chat_completions"
            base_url = "https://llm.example.invalid/v1"
            api_key = { env = "CORP_TOKEN" }
            """
        )
        (self.root / ".env").write_text(
            "CORP_TOKEN=workspace-secret\nHOOK_TOKEN=hook-secret\n",
            encoding="utf-8",
        )

        workspace_config = load_proxy_config(path, environ={})
        process_config = load_proxy_config(
            path, environ={"CORP_TOKEN": "process-secret"}
        )

        self.assertEqual(
            dict(workspace_config.providers["corp"].headers)["Authorization"],
            "Bearer workspace-secret",
        )
        self.assertEqual(workspace_config.env["HOOK_TOKEN"], "hook-secret")
        self.assertEqual(
            dict(process_config.providers["corp"].headers)["Authorization"],
            "Bearer process-secret",
        )

    def test_process_environment_overrides_dotenv_without_explicit_mapping(
        self,
    ) -> None:
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"

            [providers.corp]
            adapter = "openai_compatible"
            protocol = "openai_chat_completions"
            base_url = "https://llm.example.invalid/v1"
            api_key = { env = "HAGENCY_TEST_CORP_TOKEN" }
            """
        )
        (self.root / ".env").write_text(
            "HAGENCY_TEST_CORP_TOKEN=workspace-secret\n", encoding="utf-8"
        )

        with mock.patch.dict(
            os.environ, {"HAGENCY_TEST_CORP_TOKEN": "process-secret"}, clear=False
        ):
            config = load_proxy_config(path)

        self.assertEqual(
            dict(config.providers["corp"].headers)["Authorization"],
            "Bearer process-secret",
        )

    def test_rejects_dotenv_with_invalid_encoding(self) -> None:
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"

            [providers.corp]
            adapter = "openai_compatible"
            protocol = "openai_chat_completions"
            base_url = "https://llm.example.invalid/v1"
            """
        )
        (self.root / ".env").write_bytes(b"\xff")

        with self.assertRaisesRegex(
            ModelProxyConfigError, "could not read environment file"
        ):
            load_proxy_config(path, environ={})

    def test_openai_adapter_supplies_protocol_url_and_api_key_format(self) -> None:
        path = self.write_config(
            """
            version = 1
            default_provider = "openai"
            [providers.openai]
            adapter = "openai"
            api_key = { env = "OPENAI_API_KEY" }
            """
        )

        provider = load_proxy_config(
            path, environ={"OPENAI_API_KEY": "secret"}
        ).providers["openai"]

        self.assertEqual(provider.protocol, PROTOCOL_RESPONSES)
        self.assertEqual(provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(dict(provider.headers), {"Authorization": "Bearer secret"})
        self.assertEqual(
            provider.adapter.operation_path(PROTOCOL_CHAT), "chat/completions"
        )

    def test_adapter_is_required_and_unknown_adapter_has_extension_hint(self) -> None:
        missing = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            protocol = "openai_responses"
            base_url = "https://example.invalid/v1"
            """
        )
        with self.assertRaisesRegex(ModelProxyConfigError, "adapter.*non-empty"):
            load_proxy_config(missing)

        unknown = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            adapter = "corp_gateway"
            """
        )
        with self.assertRaisesRegex(
            ModelProxyConfigError, r"providers/corp_gateway\.py exporting ADAPTER"
        ):
            load_proxy_config(unknown)

        unsupported_version = self.write_config(
            """
            version = 2
            default_provider = "openai"
            [providers.openai]
            adapter = "openai"
            """
        )
        with self.assertRaisesRegex(ModelProxyConfigError, "version: must be 1"):
            load_proxy_config(unsupported_version)

    def test_rejects_missing_env_unknown_fields_and_model_tables(self) -> None:
        missing_env = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            adapter = "openai"
            api_key = { env = "MISSING_TOKEN" }
            """
        )
        with self.assertRaisesRegex(ModelProxyConfigError, "MISSING_TOKEN"):
            load_proxy_config(missing_env, environ={})

        model_table = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            adapter = "openai"
            protocol = "openai_responses"
            base_url = "https://example.invalid/v1"
            [providers.corp.models.foo]
            target = "bar"
            """
        )
        with self.assertRaisesRegex(ModelProxyConfigError, "unknown field: models"):
            load_proxy_config(model_table)

    def test_rejects_operation_urls_reserved_names_and_non_http_urls(self) -> None:
        cases = (
            ("v1", "https://example.invalid/v1", "invalid or reserved"),
            ("corp", "file:///tmp/provider", "absolute http"),
            ("corp", "https://example.invalid/v1?key=x", "query or fragment"),
            (
                "corp",
                "https://example.invalid/v1/chat/completions",
                "API root",
            ),
            ("corp", "https://example.invalid/v1/models", "API root"),
        )
        for name, base_url, message in cases:
            with self.subTest(name=name, base_url=base_url):
                path = self.write_config(
                    f'version = 1\ndefault_provider = "{name}"\n[providers.{name}]\n'
                    f'adapter = "openai_compatible"\nprotocol = "openai_responses"\n'
                    f'base_url = "{base_url}"\n'
                )
                with self.assertRaisesRegex(ModelProxyConfigError, message):
                    load_proxy_config(path)

    def test_rejects_non_finite_timeouts(self) -> None:
        for field, value in (
            ("hook_timeout_seconds", "nan"),
            ("connect_timeout_seconds", "inf"),
            ("idle_timeout_seconds", "-inf"),
        ):
            with self.subTest(field=field, value=value):
                path = self.write_config(
                    'version = 1\ndefault_provider = "openai"\n'
                    '[providers.openai]\nadapter = "openai"\n'
                    f"{field} = {value}\n"
                )
                with self.assertRaisesRegex(
                    ModelProxyConfigError, f"{field}.*finite positive number"
                ):
                    load_proxy_config(path)

    def test_hook_contract_is_validated_before_the_server_can_start(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "bad.py").write_text(
            "class Hook:\n"
            "    def __init__(self, init): pass\n"
            "    def authenticate(self, ctx, request): return None\n",
            encoding="utf-8",
        )
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            adapter = "openai_compatible"
            protocol = "openai_responses"
            base_url = "https://example.invalid/v1"
            hook = "bad.py"
            """
        )
        with self.assertRaisesRegex(
            ModelProxyConfigError, "authenticate must be async def"
        ):
            create_model_proxy_app(load_proxy_config(path))

    def test_hook_module_is_registered_while_postponed_dataclass_is_defined(
        self,
    ) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "typed.py").write_text(
            textwrap.dedent(
                """
                from __future__ import annotations
                from dataclasses import dataclass

                @dataclass
                class Settings:
                    value: str

                class Hook:
                    def __init__(self, init):
                        self.settings = Settings(value=init.provider)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        path = self.write_config(
            """
            version = 1
            default_provider = "corp"
            [providers.corp]
            adapter = "openai_compatible"
            base_url = "https://example.invalid/v1"
            hook = "typed.py"
            """
        )

        create_model_proxy_app(load_proxy_config(path))

    def test_models_path_validation_rejects_invalid_values(self) -> None:
        for path in ("", "/", "/models", "../models", "models/../other", "."):
            with self.subTest(path=path):
                with self.assertRaises(ProviderAdapterError):
                    ProviderAdapter(name="t", models_path=path).validate()
        ProviderAdapter(name="t", models_path=None).validate()
        ProviderAdapter(name="t", models_path="models").validate()


class ConversionTests(unittest.TestCase):
    def test_model_and_common_request_fields_are_provider_agnostic(self) -> None:
        warnings = ConversionWarnings()
        responses = convert_request(
            json.dumps(
                {
                    "model": "corp/model:preview",
                    "instructions": "Be exact",
                    "input": "hello",
                    "max_output_tokens": 123,
                    "stream": True,
                }
            ).encode(),
            PROTOCOL_RESPONSES,
            PROTOCOL_CHAT,
            warnings,
        )
        chat = json.loads(responses)
        self.assertEqual(chat["model"], "corp/model:preview")
        self.assertEqual(chat["max_completion_tokens"], 123)
        self.assertEqual(
            chat["messages"][0], {"role": "developer", "content": "Be exact"}
        )
        self.assertEqual(chat["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(chat["stream_options"], {"include_usage": True})

        back = json.loads(
            convert_request(
                json.dumps(
                    {
                        "model": "corp/model:preview",
                        "messages": [{"role": "user", "content": "hello"}],
                        "max_completion_tokens": 123,
                    }
                ).encode(),
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                ConversionWarnings(),
            )
        )
        self.assertEqual(back["model"], "corp/model:preview")
        self.assertEqual(back["max_output_tokens"], 123)

    def test_structured_output_schema_uses_each_protocols_required_shape(self) -> None:
        schema = {
            "name": "answer",
            "description": "A structured answer",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        responses = json.loads(
            convert_request(
                json.dumps(
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "hello"}],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": schema,
                        },
                    }
                ).encode(),
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                ConversionWarnings(),
            )
        )
        self.assertEqual(responses["text"]["format"], {"type": "json_schema", **schema})

        chat = json.loads(
            convert_request(
                json.dumps(
                    {
                        "model": "m",
                        "input": "hello",
                        "text": {"format": {"type": "json_schema", **schema}},
                    }
                ).encode(),
                PROTOCOL_RESPONSES,
                PROTOCOL_CHAT,
                ConversionWarnings(),
            )
        )
        self.assertEqual(
            chat["response_format"],
            {"type": "json_schema", "json_schema": schema},
        )

    def test_stream_options_preserve_obfuscation_while_requesting_chat_usage(
        self,
    ) -> None:
        chat = json.loads(
            convert_request(
                json.dumps(
                    {
                        "model": "m",
                        "input": "hello",
                        "stream": True,
                        "stream_options": {"include_obfuscation": False},
                    }
                ).encode(),
                PROTOCOL_RESPONSES,
                PROTOCOL_CHAT,
                ConversionWarnings(),
            )
        )
        self.assertEqual(
            chat["stream_options"],
            {"include_obfuscation": False, "include_usage": True},
        )

        responses = json.loads(
            convert_request(
                json.dumps(
                    {
                        "model": "m",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                        "stream_options": {
                            "include_obfuscation": False,
                            "include_usage": True,
                        },
                    }
                ).encode(),
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                ConversionWarnings(),
            )
        )
        self.assertEqual(responses["stream_options"], {"include_obfuscation": False})

    def test_tool_history_and_nonstream_responses_convert_both_directions(self) -> None:
        chat_request = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "weather"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Paris"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "weather", "parameters": {"type": "object"}},
                }
            ],
        }
        converted = json.loads(
            convert_request(
                json.dumps(chat_request).encode(),
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                ConversionWarnings(),
            )
        )
        self.assertEqual(converted["input"][1]["call_id"], "call_1")
        self.assertEqual(converted["input"][1]["arguments"], '{"city":"Paris"}')
        self.assertEqual(converted["input"][2]["output"], "sunny")
        self.assertEqual(converted["tools"][0]["name"], "weather")

        chat_response = {
            "id": "chatcmpl_1",
            "created": 7,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Paris"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        responses = json.loads(
            convert_response(
                json.dumps(chat_response).encode(),
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                ConversionWarnings(),
            )
        )
        self.assertEqual(responses["output"][0]["type"], "function_call")
        self.assertEqual(responses["usage"]["input_tokens"], 2)
        self.assertFalse(responses["parallel_tool_calls"])
        self.assertEqual(responses["tool_choice"], "auto")
        self.assertEqual(responses["tools"], [])
        self.assertEqual(
            responses["usage"]["input_tokens_details"],
            {"cache_write_tokens": 0, "cached_tokens": 0},
        )
        self.assertEqual(
            responses["usage"]["output_tokens_details"], {"reasoning_tokens": 0}
        )

        back = json.loads(
            convert_response(
                json.dumps(responses).encode(),
                PROTOCOL_RESPONSES,
                PROTOCOL_CHAT,
                ConversionWarnings(),
            )
        )
        self.assertEqual(back["choices"][0]["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(back["choices"][0]["finish_reason"], "tool_calls")

    def test_stateful_responses_features_and_audio_output_are_rejected(self) -> None:
        for body, source, target, param in (
            (
                {"model": "m", "input": "x", "previous_response_id": "resp_old"},
                PROTOCOL_RESPONSES,
                PROTOCOL_CHAT,
                "previous_response_id",
            ),
            (
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": "x"}],
                    "modalities": ["audio"],
                },
                PROTOCOL_CHAT,
                PROTOCOL_RESPONSES,
                "modalities",
            ),
        ):
            with self.subTest(param=param):
                with self.assertRaises(ConversionError) as caught:
                    convert_request(
                        json.dumps(body).encode(), source, target, ConversionWarnings()
                    )
                self.assertEqual(caught.exception.param, param)

    def test_sse_decoder_preserves_native_frames_and_handles_fragmentation(
        self,
    ) -> None:
        raw = b": keepalive\r\nevent: custom\r\nid: 7\r\ndata: one\r\ndata: two\r\n\r\n"
        decoder = SseDecoder()
        events = []
        for byte in raw:
            events.extend(decoder.feed(bytes([byte])))
        events.extend(decoder.finish())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, b"one\ntwo")
        self.assertEqual(events[0].event, "custom")
        self.assertEqual(encode_event(events[0]), raw)

    def test_stream_state_machines_emit_single_terminal_and_usage(self) -> None:
        chat = ChatToResponsesStream(ConversionWarnings())
        output = []
        output.extend(
            chat.feed(
                json_event(
                    None,
                    {
                        "id": "chatcmpl_1",
                        "created": 1,
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "he"},
                                "finish_reason": None,
                            }
                        ],
                    },
                )
            )
        )
        output.extend(
            chat.feed(
                json_event(
                    None,
                    {
                        "id": "chatcmpl_1",
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "llo"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                )
            )
        )
        output.extend(chat.feed(SseEvent(data=b"[DONE]")))
        values = [event.json() for event in output]
        self.assertEqual(
            [item["type"] for item in values].count("response.completed"), 1
        )
        completed = next(
            item["response"] for item in values if item["type"] == "response.completed"
        )
        self.assertEqual(completed["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(completed["usage"]["total_tokens"], 2)

        responses = ResponsesToChatStream(ConversionWarnings(), include_usage=True)
        chunks = []
        chunks.extend(
            responses.feed(
                json_event(
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {"id": "resp_1", "model": "m", "created_at": 1},
                    },
                )
            )
        )
        chunks.extend(
            responses.feed(
                json_event(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": "hello"},
                )
            )
        )
        chunks.extend(
            responses.feed(
                json_event(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_1",
                            "model": "m",
                            "created_at": 1,
                            "status": "completed",
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        },
                    },
                )
            )
        )
        self.assertEqual(sum(event.data == b"[DONE]" for event in chunks), 1)
        usage = [
            event.json()["usage"]
            for event in chunks
            if event.data != b"[DONE]" and event.json().get("choices") == []
        ]
        self.assertEqual(usage[0]["total_tokens"], 2)

    def test_stream_tool_fragments_keep_arguments_opaque_and_indexes_contiguous(
        self,
    ) -> None:
        chat = ChatToResponsesStream(ConversionWarnings())
        events = []
        chunks = (
            {
                "id": "chat_1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "wea"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chat_1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "ther",
                                        "arguments": '{"city":',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chat_1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"Paris"}'}}
                            ],
                            "content": "done",
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )
        for chunk in chunks:
            events.extend(chat.feed(json_event(None, chunk)))
        events.extend(chat.feed(SseEvent(data=b"[DONE]")))
        values = [event.json() for event in events]
        added = [
            value for value in values if value["type"] == "response.output_item.added"
        ]
        self.assertEqual([value["output_index"] for value in added], [0, 1])
        self.assertEqual(added[0]["item"]["name"], "weather")
        terminal = next(
            value["response"]
            for value in values
            if value["type"] == "response.completed"
        )
        self.assertEqual(terminal["output"][0]["arguments"], '{"city":"Paris"}')
        self.assertEqual(terminal["output"][1]["content"][0]["text"], "done")

        responses = ResponsesToChatStream(ConversionWarnings(), include_usage=False)
        output = []
        output.extend(
            responses.feed(
                json_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 3,
                        "item": {
                            "id": "fc_1",
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "weather",
                        },
                    },
                )
            )
        )
        output.extend(
            responses.feed(
                json_event(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 3,
                        "item_id": "fc_1",
                        "delta": '{"city":',
                    },
                )
            )
        )
        output.extend(
            responses.feed(
                json_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 3,
                        "item": {
                            "id": "fc_1",
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "weather",
                            "arguments": '{"city":"Paris"}',
                        },
                    },
                )
            )
        )
        fragments = []
        for event in output:
            value = event.json()
            for call in (
                value.get("choices", [{}])[0].get("delta", {}).get("tool_calls", [])
            ):
                arguments = call.get("function", {}).get("arguments")
                if arguments:
                    fragments.append(arguments)
        self.assertEqual("".join(fragments), '{"city":"Paris"}')


class ModelProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests: list[dict] = []
        self.behavior = self.native_json_behavior
        upstream_app = web.Application(handler_args={"auto_decompress": False})
        upstream_app.router.add_route("*", "/{tail:.*}", self.handle_upstream)
        self.upstream_server = TestServer(upstream_app)
        await self.upstream_server.start_server()
        self.proxy_client: TestClient | None = None

    async def asyncTearDown(self) -> None:
        if self.proxy_client is not None:
            await self.proxy_client.close()
        await self.upstream_server.close()
        self.tmp.cleanup()

    async def handle_upstream(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": request.query_string,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return await self.behavior(request, body)

    async def native_json_behavior(
        self, _request: web.Request, _body: bytes
    ) -> web.Response:
        return web.Response(
            status=201,
            body=b'{ "z": 1, "unknown": [3,2,1] }',
            headers={"Content-Type": "application/json", "X-Upstream": "yes"},
        )

    async def models_behavior(
        self, _request: web.Request, _body: bytes
    ) -> web.Response:
        return web.Response(
            status=200,
            body=json.dumps({"data": [{"id": "upstream-model"}]}).encode(),
            content_type="application/json",
        )

    def write_config(
        self, protocol: str, *, hook: bool = False, forward: str = ""
    ) -> Path:
        path = self.root / "hagency-model-proxy.toml"
        hook_line = 'hook = "corp.py"\n' if hook else ""
        path.write_text(
            textwrap.dedent(
                f"""
                version = 1
                default_provider = "corp"
                [providers.corp]
                adapter = "openai_compatible"
                protocol = "{protocol}"
                base_url = "{str(self.upstream_server.make_url("/api/v1")).rstrip("/")}"
                {hook_line}{forward}
                [providers.corp.headers]
                Authorization = "Bearer upstream-secret"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        return path

    async def start_proxy(
        self, protocol: str, *, hook: bool = False, forward: str = ""
    ) -> None:
        config = load_proxy_config(
            self.write_config(protocol, hook=hook, forward=forward)
        )
        self.proxy_client = TestClient(TestServer(create_model_proxy_app(config)))
        await self.proxy_client.start_server()

    async def test_native_route_preserves_entity_and_routes_without_model_logic(
        self,
    ) -> None:
        await self.start_proxy(PROTOCOL_RESPONSES)
        raw = b'{ "model": "corp/model:preview", "unknown": {"order": [2,1]} }'
        response = await self.proxy_client.post(
            "/corp/v1/responses?trace=1",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer downstream-secret",
            },
        )
        body = await response.read()

        self.assertEqual(response.status, 201)
        self.assertEqual(body, b'{ "z": 1, "unknown": [3,2,1] }')
        self.assertEqual(response.headers["X-Upstream"], "yes")
        recorded = self.requests[0]
        self.assertEqual(recorded["path"], "/api/v1/responses")
        self.assertEqual(recorded["query"], "trace=1")
        self.assertEqual(recorded["body"], raw)
        self.assertEqual(recorded["headers"]["Authorization"], "Bearer upstream-secret")

        compressed = gzip.compress(raw)
        compressed_response = await self.proxy_client.post(
            "/v1/responses",
            data=compressed,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        await compressed_response.read()
        self.assertEqual(self.requests[1]["body"], compressed)
        self.assertEqual(self.requests[1]["headers"]["Content-Encoding"], "gzip")

        extension = await self.proxy_client.get(
            "/v1/responses/resp_123/input_items?limit=2"
        )
        await extension.read()
        self.assertEqual(
            self.requests[2]["path"], "/api/v1/responses/resp_123/input_items"
        )

        escaped = await self.proxy_client.get("/v1/responses/%252E%252E/files")
        self.assertEqual(escaped.status, 400)
        self.assertEqual((await escaped.json())["error"]["code"], "invalid_path")
        self.assertEqual(len(self.requests), 3)

        missing = await self.proxy_client.post(
            "/missing/v1/responses", json={"model": "m", "input": "x"}
        )
        self.assertEqual(missing.status, 404)
        self.assertEqual((await missing.json())["error"]["code"], "unknown_provider")

    async def test_cross_protocol_create_converts_request_and_response_but_rejects_extensions(
        self,
    ) -> None:
        async def chat_behavior(_request: web.Request, _body: bytes) -> web.Response:
            return web.json_response(
                {
                    "id": "chatcmpl_1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "corp/model:preview",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                }
            )

        self.behavior = chat_behavior
        await self.start_proxy(PROTOCOL_CHAT)
        response = await self.proxy_client.post(
            "/v1/responses",
            json={
                "model": "corp/model:preview",
                "instructions": "Be terse",
                "input": "hello",
                "max_output_tokens": 12,
            },
            headers={"Content-MD5": "stale", "Digest": "sha-256=stale"},
        )
        converted_response = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests[0]["path"], "/api/v1/chat/completions")
        upstream_body = json.loads(self.requests[0]["body"])
        self.assertEqual(upstream_body["model"], "corp/model:preview")
        self.assertEqual(upstream_body["max_completion_tokens"], 12)
        self.assertEqual(upstream_body["messages"][0]["role"], "developer")
        self.assertEqual(self.requests[0]["headers"]["Accept-Encoding"], "identity")
        self.assertNotIn("Content-MD5", self.requests[0]["headers"])
        self.assertNotIn("Digest", self.requests[0]["headers"])
        self.assertEqual(converted_response["object"], "response")
        self.assertEqual(converted_response["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(converted_response["usage"]["total_tokens"], 3)

        unsupported = await self.proxy_client.get("/v1/responses/resp_1")
        self.assertEqual(unsupported.status, 404)
        self.assertEqual(
            (await unsupported.json())["error"]["code"], "unsupported_operation"
        )

        malformed = await self.proxy_client.post(
            "/v1/responses",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(
            (await malformed.json())["error"]["code"], "unsupported_conversion"
        )

        compressed = await self.proxy_client.post(
            "/v1/responses",
            data=gzip.compress(b'{"model":"m","input":"x"}'),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        self.assertEqual(compressed.status, 400)
        self.assertEqual(
            (await compressed.json())["error"]["code"],
            "unsupported_content_encoding",
        )

    async def test_cross_protocol_errors_are_sanitized_and_warnings_do_not_log_values(
        self,
    ) -> None:
        async def error_behavior(_request: web.Request, _body: bytes) -> web.Response:
            return web.json_response(
                {
                    "error": {
                        "message": "rate limited",
                        "type": "rate_limit_error",
                        "code": "rate_limit",
                        "private": "must-not-leak",
                    }
                },
                status=429,
                headers={"Retry-After": "2", "X-Request-Id": "upstream_1"},
            )

        self.behavior = error_behavior
        await self.start_proxy(PROTOCOL_CHAT)
        with self.assertLogs("hagency.model_proxy", level="WARNING") as captured:
            response = await self.proxy_client.post(
                "/v1/responses",
                json={"model": "m", "input": "hello", "stop": ["sensitive-stop-value"]},
            )
            body = await response.json()

        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertEqual(response.headers["X-Request-Id"], "upstream_1")
        self.assertEqual(body["error"]["message"], "rate limited")
        self.assertNotIn("private", body["error"])
        self.assertNotIn("must-not-leak", json.dumps(body))
        self.assertNotIn("sensitive-stop-value", "\n".join(captured.output))

    async def test_native_sse_is_byte_preserving_and_cross_sse_is_incremental(
        self,
    ) -> None:
        native_stream = (
            b': comment\n\nevent: custom\ndata: {"unknown":true}\n\ndata: [DONE]\n\n'
        )

        async def raw_stream(request: web.Request, _body: bytes) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for chunk in (native_stream[:13], native_stream[13:37], native_stream[37:]):
                await response.write(chunk)
            await response.write_eof()
            return response

        self.behavior = raw_stream
        await self.start_proxy(PROTOCOL_CHAT)
        native = await self.proxy_client.post(
            "/v1/chat/completions", json={"model": "m", "messages": [], "stream": True}
        )
        self.assertEqual(await native.read(), native_stream)

        await self.proxy_client.close()
        self.proxy_client = None

        async def chat_stream(request: web.Request, _body: bytes) -> web.StreamResponse:
            payload = (
                b'data: {"id":"chat_1","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"he"},"finish_reason":null}]}\n\n'
                b'data: {"id":"chat_1","model":"m","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for index in range(0, len(payload), 7):
                await response.write(payload[index : index + 7])
            await response.write_eof()
            return response

        self.behavior = chat_stream
        await self.start_proxy(PROTOCOL_CHAT)
        converted = await self.proxy_client.post(
            "/v1/responses", json={"model": "m", "input": "hi", "stream": True}
        )
        body = await converted.read()
        self.assertIn(b"event: response.created", body)
        self.assertIn(b'"delta":"he"', body)
        self.assertIn(b'"delta":"llo"', body)
        self.assertIn(b"event: response.completed", body)
        self.assertNotIn(b"[DONE]", body)

    async def test_converted_stream_timeout_finishes_with_response_failed(self) -> None:
        async def stalled_chat_stream(
            request: web.Request, _body: bytes
        ) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                b'data: {"id":"chat_1","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},"finish_reason":null}]}\n\n'
            )
            await asyncio.sleep(0.2)
            await response.write_eof()
            return response

        self.behavior = stalled_chat_stream
        await self.start_proxy(PROTOCOL_CHAT, forward="idle_timeout_seconds = 0.05\n")

        response = await self.proxy_client.post(
            "/v1/responses", json={"model": "m", "input": "hi", "stream": True}
        )
        body = await asyncio.wait_for(response.read(), timeout=1)

        self.assertEqual(response.status, 200)
        self.assertIn(b"event: response.created", body)
        self.assertIn(b"event: response.failed", body)
        decoder = SseDecoder()
        values = [
            event.json()
            for event in (*decoder.feed(body), *decoder.finish())
            if event.data
        ]
        created = next(value for value in values if value["type"] == "response.created")
        failed = next(value for value in values if value["type"] == "response.failed")
        self.assertGreater(failed["sequence_number"], created["sequence_number"])
        self.assertEqual(failed["response"]["model"], "m")
        self.assertEqual(failed["response"]["error"]["code"], "server_error")

    async def test_sse_mapper_removes_upstream_body_integrity_headers(self) -> None:
        async def sse_behavior(
            request: web.Request, _body: bytes
        ) -> web.StreamResponse:
            response = web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "ETag": '"upstream-body"',
                    "Content-MD5": "stale",
                }
            )
            await response.prepare(request)
            await response.write(b'data: {"type":"response.completed"}\n\n')
            await response.write_eof()
            return response

        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                from hagency_cli.model_proxy import ResponsePatch

                async def map_event(ctx, event):
                    return (event,)

                class Hook:
                    def __init__(self, init): pass

                    async def process_response(self, ctx, response):
                        return ResponsePatch(sse_mapper=map_event)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.behavior = sse_behavior
        await self.start_proxy(PROTOCOL_RESPONSES, hook=True)

        response = await self.proxy_client.post(
            "/v1/responses", json={"model": "m", "input": "hi", "stream": True}
        )
        await response.read()

        self.assertNotIn("ETag", response.headers)
        self.assertNotIn("Content-MD5", response.headers)

    async def test_provider_hook_runs_in_provider_native_order_and_signs_final_body(
        self,
    ) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                import hashlib
                from hagency_cli.model_proxy import AuthPatch, HeaderPatch, RequestPatch, ResponsePatch

                class Hook:
                    def __init__(self, init):
                        self.provider = init.provider

                    async def prepare_request(self, ctx, request):
                        body = request.json()
                        body["hooked_request"] = ctx.upstream_protocol
                        return RequestPatch(json_body=body)

                    async def authenticate(self, ctx, request):
                        digest = hashlib.sha256(request.body).hexdigest()
                        return AuthPatch(headers=HeaderPatch(set=(("X-Body-SHA256", digest),)))

                    async def process_response(self, ctx, response):
                        body = response.json()
                        body["hooked_response"] = ctx.provider
                        return ResponsePatch(json_body=body)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_RESPONSES, hook=True)
        response = await self.proxy_client.post(
            "/v1/responses", json={"model": "m", "input": "hi"}
        )
        body = await response.json()

        upstream_body = self.requests[0]["body"]
        import hashlib

        self.assertEqual(
            json.loads(upstream_body)["hooked_request"], PROTOCOL_RESPONSES
        )
        self.assertEqual(
            self.requests[0]["headers"]["X-Body-SHA256"],
            hashlib.sha256(upstream_body).hexdigest(),
        )
        self.assertEqual(body["hooked_response"], "corp")

    async def test_provider_hook_exception_is_fail_closed_and_redacted(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                class Hook:
                    def __init__(self, init): pass

                    async def authenticate(self, ctx, request):
                        raise RuntimeError("secret-hook-detail")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_RESPONSES, hook=True)
        with self.assertLogs("hagency.model_proxy", level="ERROR") as captured:
            response = await self.proxy_client.post(
                "/v1/responses",
                json={"model": "m", "input": "hi"},
            )
            raw = await response.read()

        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(raw)["error"]["code"], "provider_hook_error")
        self.assertNotIn(b"secret-hook-detail", raw)
        self.assertNotIn("secret-hook-detail", "\n".join(captured.output))
        self.assertEqual(self.requests, [])

    async def test_provider_hook_can_read_workspace_dotenv(self) -> None:
        (self.root / ".env").write_text(
            "CORP_HOOK_TOKEN=workspace-hook-secret\n", encoding="utf-8"
        )
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                from hagency_cli.model_proxy import AuthPatch, HeaderPatch

                class Hook:
                    def __init__(self, init):
                        self.token = init.env["CORP_HOOK_TOKEN"]

                    async def authenticate(self, ctx, request):
                        return AuthPatch(
                            headers=HeaderPatch(set=(("X-Corp-Token", self.token),))
                        )
                """
            ).lstrip(),
            encoding="utf-8",
        )

        await self.start_proxy(PROTOCOL_RESPONSES, hook=True)
        response = await self.proxy_client.post(
            "/v1/responses", json={"model": "m", "input": "hi"}
        )
        await response.read()

        self.assertEqual(response.status, 201)
        self.assertEqual(
            self.requests[0]["headers"]["X-Corp-Token"], "workspace-hook-secret"
        )

    async def test_credential_forwarding_requires_provider_whitelist(self) -> None:
        await self.start_proxy(
            PROTOCOL_RESPONSES,
            forward='forward_credential_headers = ["X-Api-Key"]\n',
        )
        response = await self.proxy_client.post(
            "/v1/responses",
            json={"model": "m", "input": "hi"},
            headers={
                "Authorization": "Bearer downstream",
                "X-Api-Key": "forward-me",
                "Ocp-Apim-Subscription-Key": "do-not-forward",
                "X-Amz-Security-Token": "do-not-forward",
            },
        )
        await response.read()
        self.assertEqual(
            self.requests[0]["headers"]["Authorization"], "Bearer upstream-secret"
        )
        self.assertEqual(self.requests[0]["headers"]["X-Api-Key"], "forward-me")
        self.assertNotIn("Ocp-Apim-Subscription-Key", self.requests[0]["headers"])
        self.assertNotIn("X-Amz-Security-Token", self.requests[0]["headers"])

    async def test_models_route_with_hook_fetch_models(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                class Hook:
                    def __init__(self, init): pass
                    async def fetch_models(self, ctx):
                        return ["model-a", "model-b"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_CHAT, hook=True)
        response = await self.proxy_client.get("/v1/models")
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["object"], "list")
        self.assertEqual([m["id"] for m in body["data"]], ["model-a", "model-b"])
        self.assertEqual(body["data"][0]["object"], "model")
        self.assertEqual(body["data"][0]["owned_by"], "corp")
        self.assertTrue(all(model["created"] == 0 for model in body["data"]))
        self.assertEqual(self.requests, [])

    async def test_models_route_fetch_models_none_reports_no_model_list(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                class Hook:
                    def __init__(self, init): pass
                    async def fetch_models(self, ctx):
                        return None
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_CHAT, hook=True)

        response = await self.proxy_client.get("/v1/models")
        body = await response.json()

        self.assertEqual(response.status, 404)
        self.assertEqual(body["error"]["code"], "unsupported_operation")
        self.assertEqual(
            body["error"]["message"], "provider hook returned no model list"
        )

    async def test_models_route_rejects_invalid_hook_model_ids(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                class Hook:
                    def __init__(self, init): pass
                    async def fetch_models(self, ctx):
                        return [123]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_CHAT, hook=True)

        response = await self.proxy_client.get("/v1/models")
        body = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(body["error"]["code"], "provider_hook_error")

    async def test_models_route_hook_reject_maps_to_4xx(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                from hagency_cli.model_proxy import HookReject

                class Hook:
                    def __init__(self, init): pass
                    async def fetch_models(self, ctx):
                        raise HookReject(
                            status=401,
                            code="credential_invalid",
                            public_message="bad creds",
                        )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        await self.start_proxy(PROTOCOL_CHAT, hook=True)
        response = await self.proxy_client.get("/v1/models")
        body = await response.json()
        self.assertEqual(response.status, 401)
        self.assertEqual(body["error"]["code"], "credential_invalid")
        self.assertEqual(body["error"]["message"], "bad creds")

    async def test_models_route_proxies_get_without_hook(self) -> None:
        self.behavior = self.models_behavior
        await self.start_proxy(PROTOCOL_CHAT)
        response = await self.proxy_client.get("/v1/models")
        await response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests[0]["method"], "GET")
        self.assertTrue(self.requests[0]["path"].endswith("/models"))

    async def test_models_route_runs_the_full_hook_pipeline(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "corp.py").write_text(
            textwrap.dedent(
                """
                from hagency_cli.model_proxy import (
                    AuthPatch,
                    HeaderPatch,
                    QueryPatch,
                    RequestPatch,
                    ResponsePatch,
                )

                class Hook:
                    def __init__(self, init): pass

                    async def prepare_request(self, ctx, request):
                        ctx.state["prepared"] = True
                        return RequestPatch(
                            headers=HeaderPatch(set=(("X-Prepared", "yes"),)),
                            query=QueryPatch(set=(("prepared", "yes"),)),
                        )

                    async def authenticate(self, ctx, request):
                        if not ctx.state.get("prepared"):
                            raise RuntimeError("prepare_request was skipped")
                        return AuthPatch(
                            headers=HeaderPatch(set=(("X-Auth-Token", "injected"),))
                        )

                    async def process_response(self, ctx, response):
                        body = response.json()
                        body["hooked"] = ctx.state.get("prepared")
                        return ResponsePatch(json_body=body)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.behavior = self.models_behavior
        await self.start_proxy(PROTOCOL_CHAT, hook=True)
        response = await self.proxy_client.get("/v1/models")
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests[0]["query"], "prepared=yes")
        self.assertEqual(self.requests[0]["headers"]["X-Prepared"], "yes")
        self.assertEqual(self.requests[0]["headers"]["X-Auth-Token"], "injected")
        self.assertTrue(body["hooked"])

    async def test_models_route_405_on_post(self) -> None:
        await self.start_proxy(PROTOCOL_CHAT)
        for path in ("/v1/models", "/corp/v1/models"):
            with self.subTest(path=path):
                response = await self.proxy_client.post(path)
                self.assertEqual(response.status, 405)


if __name__ == "__main__":
    unittest.main()
