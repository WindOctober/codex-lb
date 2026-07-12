from __future__ import annotations

import json
from datetime import datetime

from app.modules.codex_reset_forecast.service import CodexResetForecastService, SignalRefreshOutput


def test_codex_reset_forecast_without_active_signal_is_low(monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.probability == 0.08
    assert forecast.probability_level == "low"
    assert forecast.collection_status.refresh_enabled is False
    assert forecast.current_evidence[0].kind == "no_active_signal"
    assert [example.label for example in forecast.historical_examples] == [
        "May 17 full reset",
        "May 20 Sam/Tibo reset",
        "May 24 all-account reset",
    ]


def test_codex_reset_forecast_default_refresh_interval_is_twelve_hours():
    service = CodexResetForecastService()

    assert service.refresh_interval_seconds == 43200


def test_codex_reset_forecast_next_attempt_uses_cached_completion():
    service = CodexResetForecastService(refresh_interval_seconds=43200)
    service._snapshot["last_completed_at"] = "2026-06-13T02:14:47Z"

    delay_seconds = service._seconds_until_next_refresh_attempt(datetime(2026, 6, 13, 2, 28, 11))

    assert delay_seconds == 42396.0


def test_codex_reset_forecast_next_attempt_is_due_after_cached_interval():
    service = CodexResetForecastService(refresh_interval_seconds=43200)
    service._snapshot["last_completed_at"] = "2026-06-13T02:14:47Z"

    delay_seconds = service._seconds_until_next_refresh_attempt(datetime(2026, 6, 13, 14, 14, 47))

    assert delay_seconds == 0.0


def test_codex_reset_forecast_next_attempt_uses_newer_started_time_after_failure():
    service = CodexResetForecastService(refresh_interval_seconds=43200)
    service._snapshot["last_completed_at"] = "2026-06-13T02:14:47Z"
    service._snapshot["last_started_at"] = "2026-06-13T14:20:00Z"
    service._snapshot["last_error"] = "worker failed"

    delay_seconds = service._seconds_until_next_refresh_attempt(datetime(2026, 6, 13, 14, 30, 0))

    assert delay_seconds == 42600.0


def test_codex_reset_forecast_scores_recent_sam_trigger(monkeypatch):
    monkeypatch.setenv(
        "CODEX_RESET_FORECAST_SIGNALS_JSON",
        """
        [
          {
            "kind": "sam_trigger",
            "observed_at": "2026-05-28T06:00:00Z",
            "summary": "Sam says Tibo will reset Codex rate limits.",
            "url": "https://x.com/sama/status/example",
            "source": "manual test"
          }
        ]
        """,
    )

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.probability == 0.74
    assert forecast.probability_level == "high"
    assert forecast.current_evidence[0].kind == "sam_trigger"
    assert forecast.current_evidence[0].age_hours == 2.0


def test_codex_reset_forecast_starts_after_confirmed_reset(monkeypatch):
    monkeypatch.setenv(
        "CODEX_RESET_FORECAST_SIGNALS_JSON",
        """
        [
          {
            "kind": "tibo_ok_request",
            "observed_at": "2026-05-28T06:00:00Z",
            "summary": "Tibo replied OK to a Codex reset request.",
            "url": "https://x.com/thsottiaux/status/precursor",
            "source": "manual test"
          },
          {
            "kind": "confirmed_reset",
            "observed_at": "2026-05-28T07:00:00Z",
            "summary": "Tibo confirmed Codex limits have been reset.",
            "url": "https://x.com/thsottiaux/status/reset",
            "source": "manual test"
          }
        ]
        """,
    )

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.probability == 0.08
    assert forecast.probability_level == "low"
    assert [item.kind for item in forecast.current_evidence] == ["confirmed_reset", "no_active_signal"]
    assert "after the latest confirmed reset" in forecast.current_evidence[1].summary
    assert "latest confirmed reset" in forecast.summary


def test_codex_reset_forecast_scores_precursors_after_confirmed_reset(monkeypatch):
    monkeypatch.setenv(
        "CODEX_RESET_FORECAST_SIGNALS_JSON",
        """
        [
          {
            "kind": "confirmed_reset",
            "observed_at": "2026-05-28T07:00:00Z",
            "summary": "Tibo confirmed Codex limits have been reset.",
            "url": "https://x.com/thsottiaux/status/reset",
            "source": "manual test"
          },
          {
            "kind": "sam_trigger",
            "observed_at": "2026-05-28T07:30:00Z",
            "summary": "Sam says Tibo will reset Codex rate limits again.",
            "url": "https://x.com/sama/status/new-precursor",
            "source": "manual test"
          }
        ]
        """,
    )

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.probability == 0.74
    assert forecast.current_evidence[0].kind == "sam_trigger"
    assert [item.kind for item in forecast.current_evidence] == ["sam_trigger", "confirmed_reset"]


def test_codex_reset_forecast_treats_legacy_completed_announcement_as_boundary(monkeypatch):
    monkeypatch.setenv(
        "CODEX_RESET_FORECAST_SIGNALS_JSON",
        """
        [
          {
            "kind": "explicit_reset_announcement",
            "observed_at": "2026-05-28T06:00:00Z",
            "summary": "Tibo said limits would be reset tomorrow.",
            "url": "https://x.com/thsottiaux/status/precursor",
            "source": "legacy cache"
          },
          {
            "kind": "explicit_reset_announcement",
            "observed_at": "2026-05-28T07:00:00Z",
            "summary": "Tibo directly announced that Codex usage limits had been reset.",
            "url": "https://x.com/thsottiaux/status/reset",
            "source": "legacy cache"
          }
        ]
        """,
    )

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.probability == 0.08
    assert [item.kind for item in forecast.current_evidence] == ["confirmed_reset", "no_active_signal"]
    assert forecast.current_evidence[0].url == "https://x.com/thsottiaux/status/reset"


def test_codex_reset_forecast_retains_older_confirmed_reset_boundary(monkeypatch):
    monkeypatch.setenv(
        "CODEX_RESET_FORECAST_SIGNALS_JSON",
        """
        [
          {
            "kind": "confirmed_reset",
            "observed_at": "2026-05-28T07:00:00Z",
            "summary": "Tibo confirmed Codex limits have been reset.",
            "url": "https://x.com/thsottiaux/status/reset",
            "source": "manual test"
          }
        ]
        """,
    )

    forecast = CodexResetForecastService().get_forecast(now=datetime(2026, 5, 31, 10, 0, 0))

    assert forecast.probability == 0.08
    assert [item.kind for item in forecast.current_evidence] == ["confirmed_reset", "no_active_signal"]
    assert forecast.current_evidence[0].score == 0.0
    assert forecast.current_evidence[0].age_hours == 75.0


def test_codex_reset_forecast_live_prompt_requires_x_mcp_calls():
    prompt = CodexResetForecastService()._build_live_signal_prompt()

    assert "configured url-fetcher MCP" in prompt
    assert "mcp__url_fetcher.x_paginate_timeline for username=thsottiaux, timeline=posts" in prompt
    assert "mcp__url_fetcher.x_get_user_replies for username=thsottiaux" in prompt
    assert "mcp__url_fetcher.x_paginate_timeline for username=sama, timeline=posts" in prompt
    assert "mcp__url_fetcher.x_get_user_replies for username=sama" in prompt
    assert "Do not answer from memory, search, or assumptions." in prompt
    assert "confirmed_reset" in prompt
    assert "observed strictly after that confirmed_reset timestamp" in prompt
    assert "You must include the latest visible deterministic reset confirmation" in prompt
    assert (
        "For reset-relevant latest_items, keep latest_items.text as the concise visible English original from X."
        in prompt
    )
    assert "For unrelated latest_items, do not reproduce the source text." in prompt
    assert "populate parent only when that parent/context is necessary to understand" in prompt


def test_codex_reset_forecast_refresh_output_accepts_confirmed_reset():
    output = SignalRefreshOutput.model_validate(
        {
            "signals": [
                {
                    "kind": "confirmed_reset",
                    "observed_at": "2026-05-28T07:00:00Z",
                    "summary": "Tibo confirmed Codex limits have been reset.",
                    "url": "https://x.com/thsottiaux/status/reset",
                    "source": "x.com/@thsottiaux",
                }
            ],
            "latest_items": [],
        }
    )

    assert output.signals[0].kind == "confirmed_reset"


async def test_codex_reset_forecast_scores_cached_live_signal(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)
    cache_file = tmp_path / "reset-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "signals": [
                    {
                        "kind": "tibo_ok_request",
                        "observed_at": "2026-05-28T07:30:00Z",
                        "summary": "Tibo replied OK to a Codex reset request.",
                        "url": "https://x.com/thsottiaux/status/example",
                        "source": "x.com/@thsottiaux",
                    }
                ],
                "latest_items": [
                    {
                        "author_handle": "@thsottiaux",
                        "kind": "reply",
                        "observed_at": "2026-05-28T07:30:00Z",
                        "text": "OK",
                        "translated_text_zh": "好的",
                        "url": "https://x.com/thsottiaux/status/example",
                        "reply_to": "@codex_user",
                        "parent": {
                            "author_handle": "@codex_user",
                            "text": "Please reset Codex limits.",
                            "translated_text_zh": "请重置 Codex 限额。",
                            "url": "https://x.com/codex_user/status/parent",
                        },
                        "reset_relevance": "strong",
                        "relevance_summary": "Direct reply to a Codex reset request.",
                    }
                ],
                "refresh_in_progress": False,
                "last_started_at": "2026-05-28T07:00:00Z",
                "last_completed_at": "2026-05-28T07:35:00Z",
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    service = CodexResetForecastService(cache_file=cache_file, refresh_enabled=True, refresh_interval_seconds=3600)
    await service.start()

    forecast = service.get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))

    assert forecast.current_evidence[0].kind == "tibo_ok_request"
    assert forecast.probability == 0.68
    assert forecast.collection_status.refresh_enabled is True
    assert forecast.collection_status.last_completed_at == datetime(2026, 5, 28, 7, 35, 0)
    assert forecast.latest_x_items[0].author_handle == "@thsottiaux"
    assert forecast.latest_x_items[0].translated_text_zh == "好的"
    assert forecast.latest_x_items[0].parent is not None
    assert forecast.latest_x_items[0].parent.translated_text_zh == "请重置 Codex 限额。"
    assert forecast.latest_x_items[0].reset_relevance == "strong"


async def test_codex_reset_forecast_retains_older_strong_x_item_context(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)
    cache_file = tmp_path / "reset-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "signals": [],
                "latest_items": [
                    {
                        "author_handle": "@thsottiaux",
                        "kind": "post",
                        "observed_at": "2026-05-28T07:00:00Z",
                        "text": "The Codex usage limits have been reset.",
                        "translated_text_zh": "Codex 使用限制已重置。",
                        "url": "https://x.com/thsottiaux/status/reset",
                        "reply_to": None,
                        "parent": None,
                        "reset_relevance": "strong",
                        "relevance_summary": "已完成的 Codex 限制重置确认。",
                    },
                    {
                        "author_handle": "@sama",
                        "kind": "post",
                        "observed_at": "2026-05-28T08:00:00Z",
                        "text": "Unrelated AI update.",
                        "translated_text_zh": "无关 AI 更新。",
                        "url": "https://x.com/sama/status/unrelated",
                        "reply_to": None,
                        "parent": None,
                        "reset_relevance": "none",
                        "relevance_summary": "无重置相关性。",
                    },
                ],
                "refresh_in_progress": False,
                "last_started_at": "2026-05-28T07:00:00Z",
                "last_completed_at": "2026-05-28T07:35:00Z",
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    service = CodexResetForecastService(cache_file=cache_file, refresh_enabled=True)
    await service.start()

    forecast = service.get_forecast(now=datetime(2026, 5, 31, 10, 0, 0))

    assert [item.url for item in forecast.latest_x_items] == ["https://x.com/thsottiaux/status/reset"]
    assert forecast.latest_x_items[0].text == "The Codex usage limits have been reset."


async def test_codex_reset_forecast_worker_uses_project_local_codex_without_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    bin_dir = tmp_path / "var" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text(
        """#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    out="$1"
  fi
  shift
done
printf '{"signals":[],"latest_items":[]}' > "$out"
""",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)

    service = CodexResetForecastService(project_root=tmp_path)

    payload = await service._run_codex_job(api_key="test-key", prompt="test prompt")

    assert payload == {"signals": [], "latest_items": []}


async def test_codex_reset_forecast_worker_uses_project_local_node_for_js_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    bin_dir = tmp_path / "var" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text(
        """#!/usr/bin/env node
throw new Error("fake launcher should be handled by fake node");
""",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)
    node_bin = bin_dir / "node"
    node_bin.write_text(
        """#!/bin/sh
printf '%s' "$1" > invoked-node-with.txt
shift
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    out="$1"
  fi
  shift
done
printf '{"signals":[],"latest_items":[]}' > "$out"
""",
        encoding="utf-8",
    )
    node_bin.chmod(0o755)

    service = CodexResetForecastService(project_root=tmp_path)

    payload = await service._run_codex_job(api_key="test-key", prompt="test prompt")

    assert payload == {"signals": [], "latest_items": []}
    assert (tmp_path / "invoked-node-with.txt").read_text(encoding="utf-8") == str(codex_bin)


async def test_codex_reset_forecast_refresh_keeps_cache_when_worker_returns_no_items(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)

    async def fake_load_api_key(self: CodexResetForecastService) -> str:
        return "test-key"

    monkeypatch.setattr(CodexResetForecastService, "_load_codex_lb_api_key", fake_load_api_key)
    bin_dir = tmp_path / "var" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text(
        """#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    out="$1"
  fi
  shift
done
printf '{"signals":[],"latest_items":[]}' > "$out"
""",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)
    cache_file = tmp_path / "reset-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "signals": [],
                "latest_items": [
                    {
                        "author_handle": "@thsottiaux",
                        "kind": "post",
                        "observed_at": "2026-05-28T07:30:00Z",
                        "text": "Codex update",
                        "translated_text_zh": "Codex 更新",
                        "url": "https://x.com/thsottiaux/status/example",
                        "reply_to": None,
                        "parent": None,
                        "reset_relevance": "none",
                        "relevance_summary": "No reset relevance.",
                    }
                ],
                "refresh_in_progress": False,
                "last_started_at": "2026-05-28T07:00:00Z",
                "last_completed_at": "2026-05-28T07:35:00Z",
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    service = CodexResetForecastService(project_root=tmp_path, cache_file=cache_file, refresh_enabled=True)
    await service.start()

    await service._refresh(force=True)

    forecast = service.get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))
    assert forecast.collection_status.last_completed_at == datetime(2026, 5, 28, 7, 35, 0)
    assert forecast.collection_status.last_error == "Reset forecast worker returned no inspected X items."
    assert forecast.latest_x_items[0].text == "Codex update"


async def test_codex_reset_forecast_worker_content_filter_error_is_sanitized(tmp_path):
    raw_error = (
        "ERROR: stream disconnected before completion: Incomplete response returned, "
        'reason: content_filter {"signals":[{"summary":"raw inspected X content"}]}'
    )
    bin_dir = tmp_path / "var" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text(
        f"""#!/bin/sh
printf '%s' '{raw_error}' >&2
exit 1
""",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)

    service = CodexResetForecastService(project_root=tmp_path)

    try:
        await service._run_codex_job(api_key="test-key", prompt="test prompt")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected worker failure.")

    assert message == (
        "Reset forecast worker response was interrupted by a content filter. "
        "The previous successful cache remains in use."
    )
    assert "raw inspected X content" not in message
    assert "signals" not in message


async def test_codex_reset_forecast_refresh_stores_public_worker_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)
    raw_error = (
        '5/5 codex {"signals":[{"summary":"raw inspected X content"}]} '
        "ERROR: stream disconnected before completion: Incomplete response returned, "
        "reason: content_filter tokens used 16719"
    )

    async def fake_load_api_key(self: CodexResetForecastService) -> str:
        return "test-key"

    monkeypatch.setattr(CodexResetForecastService, "_load_codex_lb_api_key", fake_load_api_key)
    bin_dir = tmp_path / "var" / "bin"
    bin_dir.mkdir(parents=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text(
        f"""#!/bin/sh
printf '%s' '{raw_error}' >&2
exit 1
""",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)
    cache_file = tmp_path / "reset-cache.json"
    service = CodexResetForecastService(project_root=tmp_path, cache_file=cache_file, refresh_enabled=True)
    await service.start()

    await service._refresh(force=True)

    forecast = service.get_forecast(now=datetime(2026, 5, 28, 8, 0, 0))
    assert forecast.collection_status.last_error == (
        "Reset forecast worker response was interrupted by a content filter. "
        "The previous successful cache remains in use."
    )
    assert "raw inspected X content" not in cache_file.read_text(encoding="utf-8")


async def test_codex_reset_forecast_api(async_client, monkeypatch):
    monkeypatch.delenv("CODEX_RESET_FORECAST_SIGNALS_JSON", raising=False)

    response = await async_client.get("/api/codex-reset/forecast")

    assert response.status_code == 200
    payload = response.json()
    assert payload["horizonHours"] == 24
    assert payload["probabilityPercent"] >= 0
    assert payload["collectionStatus"]["refreshEnabled"] in {True, False}
    assert isinstance(payload["latestXItems"], list)
    assert payload["currentEvidence"][0]["kind"] == "no_active_signal"
    assert any(example["label"] == "May 20 Sam/Tibo reset" for example in payload["historicalExamples"])
