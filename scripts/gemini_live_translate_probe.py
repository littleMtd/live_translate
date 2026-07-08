from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_SAMPLE_PATH = PROJECT_ROOT / "logs" / "labeling_sample_phase0_eval_20260613_host_primary.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "scratch" / "analysis"
DEFAULT_MODEL = "gemini-3.5-live-translate-preview"
DEFAULT_TARGET_LANGUAGE_CODE = "zh-Hant"
PCM_RATE = 16000
PCM_WIDTH_BYTES = 2
PCM_CHANNELS = 1

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"gemini_live_translate_probe_{stamp}.jsonl"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def api_key_from_env(env_names: list[str]) -> str | None:
    _load_env_file(PROJECT_ROOT / ".env")
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def load_candidate_pool(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def select_samples(
    pool: dict[str, Any],
    *,
    limit: int,
    sample_ids: set[str] | None = None,
    buckets: set[str] | None = None,
) -> list[dict[str, Any]]:
    samples = pool.get("samples")
    if not isinstance(samples, list):
        raise ValueError("candidate pool has no samples list")

    selected = []
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        bucket = str(sample.get("phase0_bucket") or "")
        if sample_ids is not None and sample_id not in sample_ids:
            continue
        if buckets is not None and bucket not in buckets:
            continue
        selected.append(sample)
        if sample_ids is None and len(selected) >= limit:
            break

    if sample_ids is not None:
        found = {str(sample.get("sample_id") or "") for sample in selected}
        missing = sorted(sample_ids - found)
        if missing:
            raise ValueError(f"sample ids not found: {', '.join(missing)}")
    return selected[:limit] if limit > 0 else selected


def _read_wav_pcm(path: Path) -> tuple[bytes, float, dict[str, Any]]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.getnframes()
        pcm = wav.readframes(frames)
    seconds = frames / rate if rate else 0.0
    metadata = {
        "path": str(path.resolve(strict=False)),
        "exists": path.exists(),
        "channels": channels,
        "sample_rate": rate,
        "sample_width_bytes": width,
        "frames": frames,
        "seconds": seconds,
    }
    if channels != PCM_CHANNELS or rate != PCM_RATE or width != PCM_WIDTH_BYTES:
        raise ValueError(
            f"{path} must be 16kHz mono 16-bit PCM wav; "
            f"got {channels}ch {rate}Hz {width * 8}-bit"
        )
    return pcm, seconds, metadata


def build_audio_payload(
    sample: dict[str, Any],
    *,
    inter_chunk_silence_ms: int = 120,
    max_audio_seconds: float | None = 45.0,
) -> dict[str, Any]:
    chunks = sample.get("source_chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError(f"{sample.get('sample_id')} has no source_chunks")

    payload = bytearray()
    chunk_metadata = []
    total_seconds = 0.0
    silence = b"\x00" * int(PCM_RATE * PCM_WIDTH_BYTES * inter_chunk_silence_ms / 1000)
    for index, chunk in enumerate(chunks):
        path = Path(str(chunk.get("audio_path") or ""))
        pcm, seconds, metadata = _read_wav_pcm(path)
        if index:
            payload.extend(silence)
            total_seconds += inter_chunk_silence_ms / 1000
        payload.extend(pcm)
        total_seconds += seconds
        metadata.update(
            {
                "utterance_id": chunk.get("utterance_id"),
                "chunk_role": chunk.get("chunk_role"),
            }
        )
        chunk_metadata.append(metadata)

    truncated = False
    if max_audio_seconds is not None and total_seconds > max_audio_seconds:
        max_bytes = int(max_audio_seconds * PCM_RATE * PCM_WIDTH_BYTES)
        del payload[max_bytes:]
        total_seconds = max_audio_seconds
        truncated = True

    return {
        "pcm": bytes(payload),
        "total_seconds": total_seconds,
        "truncated": truncated,
        "chunks": chunk_metadata,
    }


def _text_from_transcription(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "text", "") or "")


def _usage_to_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    return {"repr": repr(value)}


def _build_live_config(types: Any, *, target_language_code: str, source_language_code: str | None) -> Any:
    # Gemini Developer API rejects AudioTranscriptionConfig.languageCodes for
    # Live sessions. Keep source_language_code in the output metadata only.
    _ = source_language_code
    input_transcription = types.AudioTranscriptionConfig()
    output_transcription = types.AudioTranscriptionConfig()

    kwargs: dict[str, Any] = {
        "responseModalities": [types.Modality.AUDIO],
        "inputAudioTranscription": input_transcription,
        "outputAudioTranscription": output_transcription,
    }
    if hasattr(types, "TranslationConfig"):
        kwargs["translationConfig"] = types.TranslationConfig(
            targetLanguageCode=target_language_code,
            echoTargetLanguage=False,
        )
    elif hasattr(types, "StreamTranslationConfig"):
        kwargs["streamTranslationConfig"] = types.StreamTranslationConfig(
            targetLanguageCode=target_language_code,
            echoTargetLanguage=False,
        )
    else:
        raise RuntimeError("google-genai SDK has no live translation config type")
    return types.LiveConnectConfig(**kwargs)


async def call_gemini_live_translate(
    *,
    api_key: str,
    model: str,
    target_language_code: str,
    source_language_code: str | None,
    pcm: bytes,
    chunk_ms: int,
    realtime_send: bool,
    receive_idle_seconds: float,
    send_audio_stream_end: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - exercised manually when SDK missing.
        raise RuntimeError("Install google-genai in the venv before running live probe") from exc

    config = _build_live_config(
        types,
        target_language_code=target_language_code,
        source_language_code=source_language_code,
    )
    client = genai.Client(api_key=api_key)
    chunk_bytes = max(PCM_RATE * PCM_WIDTH_BYTES * chunk_ms // 1000, PCM_RATE * PCM_WIDTH_BYTES // 10)
    input_transcripts: list[str] = []
    output_transcripts: list[str] = []
    output_audio_bytes = 0
    usage_metadata = None
    sent_bytes = 0
    sent_chunks = 0
    received_messages = 0
    sender_error = None
    receiver_error = None

    async with client.aio.live.connect(model=model, config=config) as session:
        sender_done = asyncio.Event()
        receiver_done = asyncio.Event()

        async def sender() -> None:
            nonlocal sent_bytes, sent_chunks, sender_error
            try:
                for offset in range(0, len(pcm), chunk_bytes):
                    payload = pcm[offset : offset + chunk_bytes]
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=payload,
                            mime_type=f"audio/pcm;rate={PCM_RATE}",
                        )
                    )
                    sent_bytes += len(payload)
                    sent_chunks += 1
                    if realtime_send:
                        await asyncio.sleep(chunk_ms / 1000)
                if send_audio_stream_end:
                    await session.send_realtime_input(audio_stream_end=True)
            except Exception as exc:
                sender_error = f"{type(exc).__name__}: {exc}"
            finally:
                sender_done.set()

        async def receiver() -> None:
            nonlocal output_audio_bytes, usage_metadata, received_messages, receiver_error
            try:
                stream = session.receive()
                while True:
                    try:
                        message = await asyncio.wait_for(anext(stream), timeout=receive_idle_seconds)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if sender_done.is_set():
                            break
                        continue
                    received_messages += 1
                    if getattr(message, "usage_metadata", None) is not None:
                        usage_metadata = _usage_to_dict(message.usage_metadata)
                    server_content = getattr(message, "server_content", None)
                    if server_content is None:
                        continue
                    input_text = _text_from_transcription(getattr(server_content, "input_transcription", None))
                    output_text = _text_from_transcription(getattr(server_content, "output_transcription", None))
                    if input_text:
                        input_transcripts.append(input_text)
                    if output_text:
                        output_transcripts.append(output_text)
                    model_turn = getattr(server_content, "model_turn", None)
                    for part in getattr(model_turn, "parts", None) or []:
                        inline_data = getattr(part, "inline_data", None)
                        data = getattr(inline_data, "data", None) if inline_data is not None else None
                        if data:
                            output_audio_bytes += len(data)
                    if getattr(server_content, "turn_complete", False):
                        break
            except Exception as exc:
                receiver_error = f"{type(exc).__name__}: {exc}"
            finally:
                receiver_done.set()

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            timeout=timeout_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )
        timed_out = bool(pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)

    return {
        "input_transcription": "".join(input_transcripts).strip(),
        "output_transcription": "".join(output_transcripts).strip(),
        "output_audio_bytes": output_audio_bytes,
        "usage_metadata": usage_metadata,
        "timed_out": timed_out,
        "diagnostics": {
            "sent_bytes": sent_bytes,
            "sent_chunks": sent_chunks,
            "sender_done": sender_done.is_set(),
            "receiver_done": receiver_done.is_set(),
            "received_messages": received_messages,
            "sender_error": sender_error,
            "receiver_error": receiver_error,
        },
    }


def build_base_record(
    *,
    pool: dict[str, Any],
    sample: dict[str, Any],
    audio_payload: dict[str, Any],
    model: str,
    target_language_code: str,
    source_language_code: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "probe_schema": 1,
        "provider": "google_gemini_live_translate",
        "model": model,
        "target_language_code": target_language_code,
        "source_language_code": source_language_code,
        "dry_run": dry_run,
        "created_at": _now_iso(),
        "candidate_pool": {
            "speaker_policy": pool.get("speaker_policy"),
            "sampling_method": (pool.get("sampling") or {}).get("method"),
        },
        "sample": {
            "sample_id": sample.get("sample_id"),
            "phase0_bucket": sample.get("phase0_bucket"),
            "run_id": sample.get("run_id"),
            "translation_event_id": sample.get("translation_event_id"),
            "translation_index": sample.get("translation_index"),
            "translation_cut_reason": sample.get("translation_cut_reason"),
            "source_utterance_ids": sample.get("source_utterance_ids"),
            "evidence_source_utterance_ids": sample.get("evidence_source_utterance_ids"),
            "source_chunk_usages": sample.get("source_chunk_usages"),
            "source_text": sample.get("source_text"),
            "baseline_target_text": sample.get("target_text"),
            "quality_flags": sample.get("quality_flags"),
        },
        "audio": {
            "total_seconds": audio_payload["total_seconds"],
            "input_pcm_bytes": len(audio_payload["pcm"]),
            "truncated": audio_payload["truncated"],
            "chunks": audio_payload["chunks"],
        },
    }


async def run_probe(args: argparse.Namespace) -> int:
    pool = load_candidate_pool(args.sample_file)
    samples = select_samples(
        pool,
        limit=args.limit,
        sample_ids=set(args.sample_id) if args.sample_id else None,
        buckets=set(args.bucket) if args.bucket else None,
    )
    if not samples:
        print("No samples selected.", file=sys.stderr)
        return 2

    api_key = None if args.dry_run else api_key_from_env(args.api_key_env)
    if not args.dry_run and not api_key:
        print(f"Missing API key. Set one of: {', '.join(args.api_key_env)}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    wrote = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for sample in samples:
            started = time.perf_counter()
            status = "dry_run" if args.dry_run else "success"
            error = None
            gemini: dict[str, Any] = {}
            try:
                audio_payload = build_audio_payload(
                    sample,
                    inter_chunk_silence_ms=args.inter_chunk_silence_ms,
                    max_audio_seconds=args.max_audio_seconds,
                )
                record = build_base_record(
                    pool=pool,
                    sample=sample,
                    audio_payload=audio_payload,
                    model=args.model,
                    target_language_code=args.target_language_code,
                    source_language_code=args.source_language_code,
                    dry_run=args.dry_run,
                )
                if not args.dry_run:
                    gemini = await call_gemini_live_translate(
                        api_key=str(api_key),
                        model=args.model,
                        target_language_code=args.target_language_code,
                        source_language_code=args.source_language_code,
                        pcm=audio_payload["pcm"],
                        chunk_ms=args.chunk_ms,
                        realtime_send=not args.no_realtime_send,
                        receive_idle_seconds=args.receive_idle_seconds,
                        send_audio_stream_end=not args.no_audio_stream_end,
                        timeout_seconds=args.timeout_seconds,
                    )
                    if gemini.get("timed_out"):
                        has_result = bool(gemini.get("input_transcription") or gemini.get("output_transcription") or gemini.get("output_audio_bytes"))
                        if has_result:
                            status = "partial"
                            error = "Timed out after receiving partial Gemini output."
                        else:
                            status = "failed"
                            error = "Timed out before receiving Gemini output."
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                record = {
                    "probe_schema": 1,
                    "provider": "google_gemini_live_translate",
                    "model": args.model,
                    "target_language_code": args.target_language_code,
                    "source_language_code": args.source_language_code,
                    "dry_run": args.dry_run,
                    "created_at": _now_iso(),
                    "sample": {
                        "sample_id": sample.get("sample_id"),
                        "phase0_bucket": sample.get("phase0_bucket"),
                    },
                }
            record["status"] = status
            record["error"] = error
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if gemini:
                record["gemini"] = gemini
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            wrote += 1
            print(
                "{sample_id} {status} audio={seconds:.2f}s latency={latency:.0f}ms".format(
                    sample_id=sample.get("sample_id"),
                    status=status,
                    seconds=(record.get("audio") or {}).get("total_seconds", 0.0),
                    latency=record["latency_ms"],
                )
            )

    print(f"Wrote {wrote} Gemini Live Translate probe records to {args.output}")
    return 1 if any(json.loads(line).get("status") == "failed" for line in args.output.read_text(encoding="utf-8").splitlines()) else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline probe for Google Gemini Live Translate on Phase 0 eval candidates."
    )
    parser.add_argument("--sample-file", type=Path, default=DEFAULT_SAMPLE_PATH, help="Phase 0 candidate JSON.")
    parser.add_argument("--output", type=Path, default=_default_output_path(), help="Output JSONL path.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum selected samples.")
    parser.add_argument("--sample-id", action="append", default=None, help="Exact sample_id to run. Repeatable.")
    parser.add_argument("--bucket", action="append", default=None, help="Restrict to phase0_bucket. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Gemini; validate selection and audio only.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini Live Translate model.")
    parser.add_argument("--target-language-code", default=DEFAULT_TARGET_LANGUAGE_CODE, help="Gemini target language code.")
    parser.add_argument("--source-language-code", default=None, help="Optional source language code hint, e.g. ko.")
    parser.add_argument(
        "--api-key-env",
        action="append",
        default=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        help="Environment variable name for API key. Repeatable.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0, help="Per-sample live session timeout.")
    parser.add_argument("--receive-idle-seconds", type=float, default=8.0, help="Stop receiving after this idle period once audio is sent.")
    parser.add_argument("--chunk-ms", type=int, default=100, help="PCM chunk size sent to Gemini.")
    parser.add_argument("--no-realtime-send", action="store_true", help="Send chunks as fast as possible instead of sleeping per chunk.")
    parser.add_argument("--no-audio-stream-end", action="store_true", help="Do not send audio_stream_end after the PCM chunks.")
    parser.add_argument("--inter-chunk-silence-ms", type=int, default=120, help="Silence inserted between source chunks.")
    parser.add_argument("--max-audio-seconds", type=float, default=45.0, help="Trim input audio above this duration.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
