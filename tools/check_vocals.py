"""Проверка фоновой музыки на голос: в библиотеку идут только треки БЕЗ слов (пение мешает слушать рассказ).

Whisper без VAD (детектор речи отбрасывает пение) по 6 отрезкам по 20 с вдоль трека. «ГОЛОС» — от двух
уверенных фраз (одна «Thank you for watching» — обычная выдумка Whisper на чистой музыке).

Запуск: .venv/bin/python tools/check_vocals.py small <трек.mp3> [<трек.mp3> …]
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from engine.slides.whisper_timings import _load_audio_16k
from faster_whisper import WhisperModel
model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
for f in sys.argv[2:]:
    t0 = time.time()
    audio = _load_audio_16k(Path(f))
    n = len(audio); win = 20 * 16000
    good = []
    for k in range(6):
        start = int((n - win) * (k + 0.5) / 6)
        chunk = audio[max(0, start): start + win]
        segs, _ = model.transcribe(chunk, beam_size=1, vad_filter=False, condition_on_previous_text=False)
        for s in segs:
            words = [w for w in s.text.replace("♪", " ").split() if sum(c.isalpha() for c in w) >= 2]
            if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0 and len(words) >= 3:
                good.append(s.text.strip())
    verdict = "ГОЛОС" if len(good) >= 2 else "без слов"
    print(f"{verdict:8} | {Path(f).name} | фраз {len(good)} | {time.time()-t0:.0f}с | {' / '.join(good[:3])[:160]}", flush=True)
