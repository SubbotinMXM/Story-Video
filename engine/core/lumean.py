"""Lumean Public API — TTS по шаблону."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from engine.core.logutil import log

DEFAULT_LUMEAN_BASE = "https://api.lumean.app/api/public"
# Шаблон «Бабкины истории 3» (ElevenLabs + Cornelius)
DEFAULT_TEMPLATE_ID = "019faa27-9a1a-70a9-a020-69f6a1e3e919"
# Голос Cornelius (ElevenLabs Voice Library)
DEFAULT_VOICE_ID = "6sFKzaJr574YWVu4UuJF"
DEFAULT_VOICE_SPEED = 1.0
VOICE_SPEED_MIN = 0.7
VOICE_SPEED_MAX = 1.2
DEFAULT_PUBLIC_OWNER_ID = (
    "a72a436938f959ab051e6a27b035db67a4b75945b4d6e8c86ed40e6e946a2e11"
)


def clamp_voice_speed(speed: float | None) -> float:
    """Ограничить speed диапазоном ElevenLabs/Lumean."""
    try:
        val = float(speed) if speed is not None else DEFAULT_VOICE_SPEED
    except (TypeError, ValueError):
        val = DEFAULT_VOICE_SPEED
    return max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, val))

# partially_completed = готов только кусок; ждать completed / result_delivered
TERMINAL_OK = frozenset({"completed", "result_delivered"})
TERMINAL_FAIL = frozenset({"failed", "compensated", "cancelled"})
_CHUNK_RE = re.compile(r"chunks[/\\](\d+)", re.I)
# ElevenLabs v3: speed в API почти не влияет → локальный atempo после скачивания
_V3_MODELS = frozenset({"eleven_v3", "eleven_v3_beta"})

# Скачивание mp3: сервер Lumean/S3 часто рвёт длинный GET посередине.
_DOWNLOAD_ATTEMPTS = 6
_DOWNLOAD_CHUNK = 256 * 1024
_DOWNLOAD_LOG_EVERY = 5 * 1024 * 1024
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=30.0)
_DOWNLOAD_RETRY_STATUS = frozenset({408, 416, 429, 500, 502, 503, 504})

# Поллинг TTS: пол 20 мин, 2× оценка длительности речи (~1000 симв/мин), потолок 90 мин
_TTS_WAIT_FLOOR_SEC = 20 * 60
_TTS_WAIT_CAP_SEC = 90 * 60
_TTS_CHARS_PER_MIN = 1000
_TTS_WAIT_SPEECH_MULT = 2.0


def tts_wait_timeout_sec(text_chars: int) -> float:
    """Таймаут ожидания заказа: 2× оценка длины речи, clamp 20–90 мин."""
    n = max(0, int(text_chars))
    speech_sec = (n / _TTS_CHARS_PER_MIN) * 60.0
    return float(
        min(
            _TTS_WAIT_CAP_SEC,
            max(_TTS_WAIT_FLOOR_SEC, speech_sec * _TTS_WAIT_SPEECH_MULT),
        )
    )


@dataclass
class LumeanTemplate:
    id: str
    name: str
    service_key: str | None = None


class LumeanError(RuntimeError):
    pass


class LumeanOrderFailed(LumeanError):
    """Заказ закончился неуспешно на стороне Lumean (failed/cancelled)."""


class LumeanClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_LUMEAN_BASE) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "X-API-KEY": api_key.strip(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(120.0, connect=30.0),
            trust_env=False,
        )
        # из последнего create_tts_order — для post-process speed на v3
        self._last_tts_speed = 1.0
        self._last_tts_model = ""

    def close(self) -> None:
        self._http.close()

    def ping(self) -> str:
        """Лёгкая проверка ключа: GET /templates."""
        log("Lumean PING → GET /templates")
        t0 = time.monotonic()
        try:
            templates = self.list_templates()
            ms = int((time.monotonic() - t0) * 1000)
            msg = f"ok, шаблонов: {len(templates)}"
            log(f"Lumean PING OK ({ms} ms): {msg}")
            return msg
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            log(f"Lumean PING FAIL ({ms} ms): {type(exc).__name__}: {exc}")
            raise

    def list_templates(self) -> list[LumeanTemplate]:
        """GET /templates — корневые шаблоны аккаунта."""
        log("Lumean GET /templates")
        data = self._request("GET", "/templates")
        items = data if isinstance(data, list) else []
        out: list[LumeanTemplate] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("id") or "").strip()
            if not tid:
                continue
            out.append(
                LumeanTemplate(
                    id=tid,
                    name=str(raw.get("name") or tid),
                    service_key=raw.get("service_key"),
                )
            )
        log(f"Lumean templates: {len(out)}")
        return out

    def resolve_template_id(self, preferred: str | None = None) -> str:
        """Если preferred мёртвый/пустой — берём первый живой шаблон аккаунта."""
        preferred = (preferred or "").strip()
        templates = self.list_templates()
        if not templates:
            raise LumeanError(
                "В Lumean нет шаблонов. Создай TTS-шаблон в кабинете."
            )
        by_id = {t.id: t for t in templates}
        if preferred and preferred in by_id:
            log(f"Lumean template OK: {by_id[preferred].name} [{preferred}]")
            return preferred
        chosen = templates[0]
        if preferred:
            log(
                f"Lumean template {preferred} не найден → "
                f"беру «{chosen.name}» [{chosen.id}]"
            )
        else:
            log(f"Lumean template default: {chosen.name} [{chosen.id}]")
        return chosen.id

    def get_template(self, template_id: str) -> dict:
        """GET /templates/{id} — актуальный config шаблона (speed и т.д.)."""
        tid = template_id.strip()
        log(f"Lumean GET /templates/{tid}")
        data = self._request("GET", f"/templates/{tid}")
        if not isinstance(data, dict):
            raise LumeanError(f"Неожиданный ответ шаблона: {data!r}")
        return data

    def create_tts_order(
        self,
        *,
        template_id: str,
        input_text: str,
        voice_id: str | None = None,
        public_owner_id: str | None = None,
        speed: float | None = None,
    ) -> str:
        voice_id = (voice_id or DEFAULT_VOICE_ID).strip()
        public_owner_id = (public_owner_id or DEFAULT_PUBLIC_OWNER_ID).strip()
        template_id = template_id.strip()

        # Тянем актуальный config с сайта (без локального кэша).
        tpl = self.get_template(template_id)
        tts = ((tpl.get("config") or {}).get("tts_settings") or {})
        if not isinstance(tts, dict):
            tts = {}
        model_id = str(tts.get("model_id") or "").strip()
        vs_raw = tts.get("voice_settings")
        voice_settings = dict(vs_raw) if isinstance(vs_raw, dict) else {}
        # UI/override имеет приоритет над speed шаблона.
        if speed is None:
            try:
                speed = float(voice_settings.get("speed", DEFAULT_VOICE_SPEED) or DEFAULT_VOICE_SPEED)
            except (TypeError, ValueError):
                speed = DEFAULT_VOICE_SPEED
        speed = clamp_voice_speed(speed)
        voice_settings["speed"] = speed
        if "stability" not in voice_settings:
            voice_settings["stability"] = 0.5

        # Без advanced_voice_settings=true Lumean часто игнорит speed/style.
        # voice_settings копируем явно из шаблона, чтобы UI-правки сайта доезжали.
        body: dict = {
            "template_id": template_id,
            "input_text": input_text,
            "config_override": {
                "tts_settings": {
                    "voice_id": voice_id,
                    "public_owner_id": public_owner_id,
                    "advanced_voice_settings": True,
                    "voice_settings": voice_settings,
                }
            },
        }
        self._last_tts_speed = speed
        self._last_tts_model = model_id
        log(
            f"Lumean POST /orders template_id={template_id} "
            f"voice_id={voice_id} model={model_id or '?'} "
            f"speed={speed} advanced=1 chars={len(input_text)}"
        )
        if model_id in _V3_MODELS and abs(speed - 1.0) >= 0.01:
            log(
                f"Lumean: {model_id} почти не применяет speed — "
                f"после скачивания сделаю atempo={speed}"
            )
        data = self._request("POST", "/orders", json_body=body)
        order_id = str((data or {}).get("id") or "").strip()
        if not order_id:
            raise LumeanError(f"Нет order id в ответе: {data!r}")
        log(f"Lumean order created: {order_id} status={(data or {}).get('status')}")
        return order_id

    def get_order(self, order_id: str) -> dict:
        data = self._request("GET", f"/orders/{order_id}")
        if not isinstance(data, dict):
            raise LumeanError(f"Неожиданный ответ заказа: {data!r}")
        return data

    def wait_order(
        self,
        order_id: str,
        *,
        poll_sec: float = 3.0,
        timeout_sec: float = 3600.0,
        on_status=None,
        cancel=None,
    ) -> dict:
        from engine.core.cancel import CancelledError

        t0 = time.monotonic()
        while True:
            if cancel is not None:
                cancel.check()
            order = self.get_order(order_id)
            status = str(order.get("status") or "")
            if on_status:
                on_status(status)
            log(f"Lumean order {order_id} status={status}")
            if status in TERMINAL_OK:
                return order
            if status in TERMINAL_FAIL:
                raise LumeanOrderFailed(f"Заказ завершился со статусом {status}")
            if time.monotonic() - t0 > timeout_sec:
                raise LumeanError(f"Таймаут ожидания заказа ({timeout_sec:.0f}s)")
            # sleep кусками, чтобы быстрее реагировать на Стоп
            left = poll_sec
            while left > 0:
                if cancel is not None and cancel.is_cancelled():
                    raise CancelledError("Остановлено пользователем")
                step = min(0.25, left)
                time.sleep(step)
                left -= step

    def storage_url(self, path: str) -> str:
        data = self._request("POST", "/storage/url", json_body={"path": path})
        url = str((data or {}).get("url") or "").strip()
        if not url:
            raise LumeanError(f"Нет url в storage: {data!r}")
        return url

    def download_order_audio(self, order: dict, dest: Path) -> Path:
        result = order.get("result") or {}
        files = [str(p) for p in (result.get("files") or []) if str(p).strip()]
        if not files:
            raise LumeanError("В заказе нет result.files[]")
        files = _sort_chunk_paths(files)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if len(files) == 1:
            self._download_storage_file(files[0], dest)
        else:
            log(f"Lumean чанков аудио: {len(files)} — скачиваю и склеиваю…")
            with tempfile.TemporaryDirectory(prefix="lumean-chunks-") as tmp:
                tmp_dir = Path(tmp)
                parts: list[Path] = []
                for i, storage_path in enumerate(files):
                    part = tmp_dir / f"chunk_{i:04d}.mp3"
                    self._download_storage_file(storage_path, part)
                    parts.append(part)
                _concat_audio_files(parts, dest)
            log(f"Lumean склейка готова: {dest} ({dest.stat().st_size} bytes)")

        self._apply_v3_speed_fix(dest)
        return dest

    def _apply_v3_speed_fix(self, dest: Path) -> None:
        """v3 игнорирует voice_settings.speed — дотягиваем ffmpeg atempo."""
        speed = float(getattr(self, "_last_tts_speed", 1.0) or 1.0)
        model = str(getattr(self, "_last_tts_model", "") or "")
        if model not in _V3_MODELS:
            return
        if abs(speed - 1.0) < 0.01:
            return
        from engine.compositor.utils import find_ffmpeg

        ffmpeg = find_ffmpeg()
        # суффикс должен остаться .mp3 — иначе ffmpeg не угадает muxer
        tmp = dest.with_name(dest.stem + ".speed" + dest.suffix)
        # atempo: >1 быстрее, <1 медленнее — как ElevenLabs speed
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(dest),
            "-filter:a",
            f"atempo={speed:.4f}",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(tmp),
        ]
        log(f"Lumean speed-fix atempo={speed:.4f} model={model}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 32:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise LumeanError(
                "Не удалось применить speed к аудио:\n"
                + (proc.stderr or "")[-600:]
            )
        tmp.replace(dest)
        log(f"Lumean speed-fix OK: {dest} ({dest.stat().st_size} bytes)")

    def _download_storage_file(self, storage_path: str, dest: Path) -> Path:
        log(f"Lumean storage path={storage_path}")
        part = dest.with_name(dest.name + ".part")
        last_exc: Exception | None = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            url = self.storage_url(storage_path)
            have = part.stat().st_size if part.is_file() else 0
            extra = f", уже {have} bytes" if have else ""
            log(
                f"Lumean скачиваю аудио… "
                f"попытка {attempt}/{_DOWNLOAD_ATTEMPTS}{extra}"
            )
            try:
                _stream_download(url, part, resume_from=have)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                code = exc.response.status_code
                log(f"Lumean download HTTP {code}: {exc}")
                if code not in _DOWNLOAD_RETRY_STATUS:
                    part.unlink(missing_ok=True)
                    raise LumeanError(f"Скачивание аудио: HTTP {code}") from exc
                if code == 416:
                    part.unlink(missing_ok=True)
                time.sleep(min(2 ** (attempt - 1), 15))
                continue
            except (httpx.TransportError, OSError) as exc:
                last_exc = exc
                log(f"Lumean download оборвался: {exc}")
                time.sleep(min(2 ** (attempt - 1), 15))
                continue
            part.replace(dest)
            log(f"Lumean файл сохранён: {dest} ({dest.stat().st_size} bytes)")
            return dest
        raise LumeanError(
            f"Не удалось скачать аудио за {_DOWNLOAD_ATTEMPTS} попыток: {last_exc}"
        ) from last_exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ):
        try:
            resp = self._http.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            log(f"Lumean HTTP error: {exc}")
            raise LumeanError(f"Сеть Lumean: {exc}") from exc

        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text[:500]}

        if resp.status_code >= 400:
            msg = payload.get("message") if isinstance(payload, dict) else resp.text
            reason = payload.get("reason") if isinstance(payload, dict) else None
            errors = payload.get("errors") if isinstance(payload, dict) else None
            detail = ""
            if isinstance(errors, dict) and errors:
                parts = []
                for k, v in errors.items():
                    if isinstance(v, list):
                        parts.append(f"{k}: {'; '.join(str(x) for x in v)}")
                    else:
                        parts.append(f"{k}: {v}")
                detail = " | " + "; ".join(parts)
            log(f"Lumean ERR {resp.status_code}: {msg}{detail} reason={reason}")
            raise LumeanError(
                f"Lumean {resp.status_code}: {msg}{detail}"
                + (f" ({reason})" if reason else "")
            )

        if isinstance(payload, dict) and payload.get("success") is False:
            raise LumeanError(str(payload.get("message") or payload))

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload


def _stream_download(url: str, part: Path, *, resume_from: int) -> None:
    """Стрим в .part. Range, если уже есть кусок; 200 без Range — перезапись."""
    headers: dict[str, str] = {}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"
    with httpx.Client(
        timeout=_DOWNLOAD_TIMEOUT,
        trust_env=False,
        follow_redirects=True,
    ) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416 and resume_from > 0:
                part.unlink(missing_ok=True)
                raise httpx.HTTPStatusError(
                    "range not satisfiable",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            append = resp.status_code == 206 and resume_from > 0
            base = resume_from if append else 0
            expected = _download_expected_size(resp, base=base)
            mode = "ab" if append else "wb"
            got = 0
            last_log = base
            fh = None
            try:
                for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    if fh is None:
                        # wb только с первым байтом — иначе оборванный GET
                        # затрёт уже скачанный .part до того, как придут данные.
                        fh = part.open(mode)
                    fh.write(chunk)
                    got += len(chunk)
                    total = base + got
                    if total - last_log >= _DOWNLOAD_LOG_EVERY:
                        last_log = total
                        if expected:
                            log(f"Lumean {total}/{expected} bytes")
                        else:
                            log(f"Lumean {total} bytes")
            finally:
                if fh is not None:
                    fh.close()
            if got == 0 and not append:
                raise httpx.RemoteProtocolError("пустой ответ при скачивании")
            final = part.stat().st_size if part.is_file() else 0
            if expected is not None and final != expected:
                raise httpx.RemoteProtocolError(
                    f"неполный файл: {final} из {expected}"
                )


def _download_expected_size(resp: httpx.Response, *, base: int) -> int | None:
    content_range = resp.headers.get("content-range") or ""
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        return int(match.group(1))
    raw_len = resp.headers.get("content-length")
    if raw_len and raw_len.isdigit():
        size = int(raw_len)
        if resp.status_code == 206:
            return base + size
        return size
    return None


def _sort_chunk_paths(paths: list[str]) -> list[str]:
    def key(p: str) -> tuple[int, str]:
        m = _CHUNK_RE.search(p)
        return (int(m.group(1)), p) if m else (10**9, p)

    return sorted(paths, key=key)


def _concat_audio_files(parts: list[Path], dest: Path) -> None:
    from engine.compositor.utils import find_ffmpeg

    if not parts:
        raise LumeanError("Нет чанков для склейки")
    if len(parts) == 1:
        dest.write_bytes(parts[0].read_bytes())
        return

    ffmpeg = find_ffmpeg()
    list_file = parts[0].parent / "concat.txt"
    # concat demuxer: пути с escaped single quotes
    lines = []
    for p in parts:
        escaped = str(p).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # fallback: re-encode если контейнеры/битрейты разъехались
        cmd_re = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(dest),
        ]
        proc2 = subprocess.run(cmd_re, capture_output=True, text=True)
        if proc2.returncode != 0:
            raise LumeanError(
                "Не удалось склеить чанки Lumean:\n"
                + (proc2.stderr or proc.stderr or "")[-800:]
            )


def synthesize_resumable(
    *,
    api_key: str,
    text: str,
    template_id: str,
    voice_id: str,
    speed: float | None,
    dest: Path,
    state_file: Path,
    cancel=None,
    on_progress=None,
) -> Path:
    """Озвучка с защитой от двойной оплаты.

    id заказа пишем в state_file сразу после создания. Если процесс упал
    (таймаут, сеть, закрыли приложение), повторный вызов с тем же текстом
    дожидается и скачивает ЭТОТ ЖЕ заказ, а не создаёт новый.
    """

    def progress(pct: float, msg: str) -> None:
        log(msg)
        if on_progress:
            on_progress(pct, msg)

    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state: dict = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    if state.get("text_sha") != text_sha or state.get("voice_id") != voice_id:
        state = {}

    client = LumeanClient(api_key)
    if cancel is not None:
        cancel.register(client)
    try:
        order: dict | None = None
        order_id = str(state.get("order_id") or "")
        if order_id:
            client._last_tts_speed = float(state.get("speed") or 1.0)
            client._last_tts_model = str(state.get("model") or "")
            progress(0.35, f"Озвучка: продолжаю заказ {order_id[:8]}… (без повторной оплаты)")
            try:
                order = client.wait_order(
                    order_id,
                    timeout_sec=tts_wait_timeout_sec(len(text)),
                    on_status=lambda st: progress(0.5, f"Озвучка: {st}"),
                    cancel=cancel,
                )
            except LumeanOrderFailed as exc:
                log(f"Lumean: прошлый заказ {order_id} не удался ({exc}) — создаю новый")
                order = None
        if order is None:
            if cancel is not None:
                cancel.check()
            tid = client.resolve_template_id(template_id or DEFAULT_TEMPLATE_ID)
            if cancel is not None:
                cancel.check()
            order_id = client.create_tts_order(
                template_id=tid, input_text=text, voice_id=voice_id, speed=speed
            )
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                json.dumps(
                    {
                        "order_id": order_id,
                        "text_sha": text_sha,
                        "voice_id": voice_id,
                        "speed": client._last_tts_speed,
                        "model": client._last_tts_model,
                    }
                ),
                encoding="utf-8",
            )
            progress(0.35, f"Озвучка: заказ {order_id[:8]}…")
            order = client.wait_order(
                order_id,
                timeout_sec=tts_wait_timeout_sec(len(text)),
                on_status=lambda st: progress(0.5, f"Озвучка: {st}"),
                cancel=cancel,
            )
        if cancel is not None:
            cancel.check()
        progress(0.6, "Озвучка: скачиваю аудио…")
        part = dest.with_name(dest.stem + ".part" + dest.suffix)
        client.download_order_audio(order, part)
        part.replace(dest)
    finally:
        client.close()
    state_file.unlink(missing_ok=True)
    return dest
