# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import json as pyjson
import os
from dataclasses import dataclass
from typing import Any, Literal

import requests
from flask import current_app

from superset.commands.chart.data.get_data_command import ChartDataCommand
from superset.common.chart_data import ChartDataResultFormat, ChartDataResultType
from superset.exceptions import SupersetException
from superset.models.dashboard import Dashboard
from superset.models.slice import Slice


ChatRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ReportChatMessage:
    role: ChatRole
    content: str


class _DummyMcpContext:
    """Minimal async context shim (for optional MCP usage).

    We intentionally do not depend on MCP tool decorators during normal Superset
    requests. This class exists to isolate optional calls in case future refactors
    use MCP tooling in-process.
    """

    async def info(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    async def debug(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    async def warning(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    async def error(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    async def report_progress(  # noqa: ANN401
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        return None


def _get_external_llm_config() -> tuple[str, str, str]:
    """Read external LLM config from Flask config or environment variables."""
    # OpenAI-compatible defaults
    api_base_url = current_app.config.get(
        "EXTERNAL_LLM_API_BASE_URL",
        os.getenv("EXTERNAL_LLM_API_BASE_URL", "https://api.openai.com/v1/chat/completions"),
    )
    api_key = current_app.config.get(
        "EXTERNAL_LLM_API_KEY",
        os.getenv("EXTERNAL_LLM_API_KEY"),
    )
    model = current_app.config.get(
        "EXTERNAL_LLM_MODEL",
        os.getenv("EXTERNAL_LLM_MODEL", "gpt-4o-mini"),
    )

    if not api_key:
        raise RuntimeError(
            "Missing external LLM API key. Set EXTERNAL_LLM_API_KEY (env) or "
            "EXTERNAL_LLM_API_KEY in Superset config."
        )
    return str(api_base_url), str(api_key), str(model)


def _call_openai_compatible_chat(
    *, messages: list[dict[str, str]], max_tokens: int, temperature: float
) -> str:
    url, api_key, _model = _get_external_llm_config()

    # Some providers expect `model` at top-level. We keep it in request for maximum
    # compatibility by re-reading model from config.
    _model = current_app.config.get(
        "EXTERNAL_LLM_MODEL",
        os.getenv("EXTERNAL_LLM_MODEL", "gpt-4o-mini"),
    )

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": _model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=current_app.config.get("EXTERNAL_LLM_TIMEOUT", 60),
    )
    if not resp.ok:
        raise RuntimeError(
            f"External LLM request failed: status={resp.status_code} body={resp.text}"
        )

    data = resp.json()
    # OpenAI-compatible response shape
    if isinstance(data, dict):
        choices = data.get("choices")
        if (
            isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("message"), dict)
        ):
            content = choices[0]["message"].get("content")
            if isinstance(content, str):
                return content

        # Common alternative shapes
        if isinstance(data.get("output_text"), str):
            return str(data["output_text"])

    raise RuntimeError("External LLM response did not include message content.")


def _chart_to_prompt_context(chart: Slice) -> dict[str, Any]:
    """Convert a chart/slice to compact context for prompting."""
    return {
        "slice_id": chart.id,
        "slice_name": chart.slice_name,
        "viz_type": chart.viz_type,
        "datasource_name": chart.datasource_name,
        "datasource_type": chart.datasource_type,
        # slice.params can be large; we only include the already-normalized
        # form_data (best effort) to help the LLM reason about intent.
        "form_data": chart.form_data if chart.form_data else None,
    }


def _run_chart_query_for_chat(chart: Slice, *, row_limit: int) -> dict[str, Any]:
    """Run a lightweight chart query suitable for LLM summarization."""
    query_context = chart.get_query_context()
    if query_context is None:
        return {"error": "missing_query_context"}

    # Restrict payload size: samples are enough for reasoning.
    query_context.result_type = ChartDataResultType.SAMPLES
    query_context.result_format = ChartDataResultFormat.JSON

    # Force row limit on each query object when possible.
    for query_obj in query_context.queries:
        query_obj.row_limit = row_limit

    # Execute query.
    chart_data_command = ChartDataCommand(query_context)
    chart_data_command.validate()
    result = chart_data_command.run()

    # We keep only what LLM needs: the list of query results.
    return {
        "chart_id": chart.id,
        "queries": result.get("queries", []),
    }


def generate_report_chat_response(
    *,
    dashboard_id_or_slug: str,
    messages: list[ReportChatMessage],
    max_slices: int = 3,
    chart_row_limit: int = 20,
    max_tokens: int = 700,
    temperature: float = 0.2,
) -> str:
    """Generate a chat response grounded in dashboard/chart data."""
    dashboard = Dashboard.get(dashboard_id_or_slug)
    if not dashboard:
        raise RuntimeError("Dashboard not found.")

    dashboard.raise_for_access()

    # Choose slices to query. This is intentionally simple for MVP.
    slices = list(dashboard.slices)
    slices = slices[:max_slices]

    chart_context: list[dict[str, Any]] = []
    for chart in slices:
        try:
            context = _chart_to_prompt_context(chart)
            context["chart_data"] = _run_chart_query_for_chat(
                chart, row_limit=chart_row_limit
            )
            chart_context.append(context)
        except SupersetException as ex:
            chart_context.append(
                {
                    **_chart_to_prompt_context(chart),
                    "chart_data_error": str(ex),
                }
            )

    # Compose LLM messages. We include Superset context as system prompt and
    # forward the user's conversation for continuity.
    user_last = next(
        (m.content for m in reversed(messages) if m.role == "user"), ""
    ).strip()

    system_prompt = (
        "Bạn là trợ lý phân tích dữ liệu tích hợp với Apache Superset. "
        "Hãy trả lời câu hỏi của người dùng dựa trên dữ liệu Superset mà bạn được cung cấp. "
        "Nếu dữ liệu không đủ, hãy nói rõ giới hạn. Trả lời bằng tiếng Việt. "
        "Khi bạn đưa ra số liệu, hãy bám sát dữ liệu JSON được cung cấp. "
        "Tránh bịa đặt.\n\n"
        f"DASHBOARD: id_or_slug={dashboard_id_or_slug}, title={dashboard.dashboard_title}\n"
        f"CHART_CONTEXT (truy vấn mẫu cho từng chart/slice):\n{pyjson.dumps(chart_context, ensure_ascii=False)[:80000]}\n"
    )

    llm_messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    llm_messages.extend({"role": m.role, "content": m.content} for m in messages)

    # Ensure we don't send empty messages to the provider.
    if not llm_messages[-1]["content"]:
        llm_messages[-1]["content"] = user_last

    return _call_openai_compatible_chat(
        messages=llm_messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

