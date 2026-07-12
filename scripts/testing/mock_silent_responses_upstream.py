from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}\n\n"


@dataclass
class _RequestStats:
    models_requests: int = 0
    responses_requests: int = 0
    response_started_at: list[float] = field(default_factory=list)
    response_completed_at: list[float] = field(default_factory=list)

    def as_payload(self, *, silence_seconds: float) -> dict[str, object]:
        return {
            "models_requests": self.models_requests,
            "responses_requests": self.responses_requests,
            "response_started_at": self.response_started_at,
            "response_completed_at": self.response_completed_at,
            "silence_seconds": silence_seconds,
        }


def create_app(*, silence_seconds: float) -> FastAPI:
    app = FastAPI()
    started_at = time.monotonic()
    stats = _RequestStats()

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "uptime_seconds": time.monotonic() - started_at}

    @app.get("/stats")
    async def read_stats() -> dict[str, object]:
        return stats.as_payload(silence_seconds=silence_seconds)

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        stats.models_requests += 1
        return {"object": "list", "data": [{"id": "gpt-5.6-sol", "object": "model"}]}

    @app.post("/v1/responses")
    async def responses(request: Request) -> StreamingResponse:
        await request.json()
        stats.responses_requests += 1
        stats.response_started_at.append(time.monotonic() - started_at)
        response_id = f"resp_keepalive_{uuid4().hex}"
        message_id = f"msg_keepalive_{uuid4().hex}"

        async def stream() -> AsyncIterator[str]:
            await asyncio.sleep(silence_seconds)
            yield _sse({"type": "response.created", "response": {"id": response_id}})
            yield _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": message_id,
                        "content": [{"type": "output_text", "text": "KEEPALIVE_OK"}],
                    },
                }
            )
            yield _sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "usage": {
                            "input_tokens": 1,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 1,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 2,
                        },
                    },
                }
            )
            stats.response_completed_at.append(time.monotonic() - started_at)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--silence-seconds", type=float, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_app(silence_seconds=args.silence_seconds),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
