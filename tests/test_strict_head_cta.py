"""Строгие режимы: начало 2 мин — фото и видео под фразы; призыв подписаться — видео по теме."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.slides import strict_media as sm
from engine.slides.story_context import StoryContext
from engine.slides.theme_plan import ThemePlan

PLAN = ThemePlan(summary="Mongolia documentary", context=StoryContext(subject="place"),
                 video_queries=["Mongolian steppe horses", "Ulaanbaatar city"], image_queries=["yurt"])


class CtaTests(unittest.TestCase):
    def test_detection(self) -> None:
        self.assertTrue(sm.is_cta("Подпишитесь на канал и ставьте лайк"))
        self.assertTrue(sm.is_cta("Suscríbete y activa la campanita"))
        self.assertFalse(sm.is_cta("В 1919 году он подписал договор"))

    def test_queries_mark_cta_by_llm_or_keywords(self) -> None:
        beats = [sm.Beat(0, 4, "Монголия — страна степей"), sm.Beat(4, 8, "Подпишитесь на канал"),
                 sm.Beat(8, 12, "Поддержите нас"), sm.Beat(130, 134, "Юрты кочевников", shot=0),
                 sm.Beat(134, 138, "Юрты кочевников", shot=1)]
        client = mock.Mock()
        client.complete.return_value = json.dumps({"queries": ["Mongolian steppe", "x", "CTA", "nomad yurts"]})
        q = sm._plan_queries(client, PLAN, beats, None, all_beats=True)
        self.assertEqual(q[0], "Mongolian steppe")
        self.assertTrue(q[1].startswith(sm.CTA_MARK), "ключевые слова")
        self.assertTrue(q[2].startswith(sm.CTA_MARK), "LLM ответила CTA")
        self.assertEqual(q[3], "nomad yurts")
        self.assertEqual(q[4], "nomad yurts", "второй кадр куска — тот же запрос, без отдельного вопроса LLM")
        sent = client.complete.call_args.kwargs["user"]
        self.assertEqual(sent.count("Юрты кочевников"), 1)

    def test_cta_beat_gets_theme_video(self) -> None:
        calls = []

        def fake_try(*a, **kw):
            calls.append((a[4], kw["prefer_video"], kw["thematic"]))
            return {"kind": "video", "fit": 7, "weak": False}, 0

        with mock.patch.object(sm, "_try_kind", fake_try):
            sm._pick_beat(None, {}, PLAN, sm.Beat(4, 8, "Подпишитесь"), sm.CTA_MARK + "Mongolian steppe horses",
                          index=1, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(calls, [("Mongolian steppe horses", True, True)])


class HeadMixTests(unittest.TestCase):
    def test_head_beats_mix_photo_and_video_strictly(self) -> None:
        seen = []

        def fake_mixed(*a, **kw):
            seen.append((kw["video_query"], kw["image_query"], kw["thematic"]))
            return None, 0, False

        with mock.patch.object(sm, "_pick_mixed", fake_mixed):
            sm._pick_beat(None, {}, PLAN, sm.Beat(10, 14, "Степь"), "steppe at dawn",
                          index=2, trials=Path("."), frames=Path("."), pool=Path("."), mix=mock.Mock())
        self.assertEqual(seen, [("steppe at dawn", "steppe at dawn", False)], "начало — строго под фразу, фото или видео")

    def test_alternation_gives_about_half_video(self) -> None:
        order = [(i % 2 == 0) for i in range(30)]  # _pick_mixed: чётный — видео первым
        self.assertEqual(sum(order), 15)

class CtaSpanTests(unittest.TestCase):
    """Призыв, разрезанный между кусками, и финальный призыв без «подпишитесь» — кадр по теме ролика."""

    def _words(self, text: str, start: float = 0.0, step: float = 0.4):
        from engine.slides.whisper_timings import WordTiming

        out, t = [], start
        for w in text.split():
            out.append(WordTiming(w, t, t + step * 0.8))
            t += step
        return out

    def test_split_cta_and_final_cta(self) -> None:
        from engine.slides import strict_media as sm

        story = "Города строили одинаковые кварталы, похожих друг на друга. "
        cta1 = "Дорогие зрители, поставьте лайк этому видео и подпишитесь на наш канал, также включив колокольчик уведомлений. А теперь продолжим. "
        middle = "Река каждый год приносила новые наводнения в долину. "
        cta2 = ("Дамы и господа, сейчас на экране перед вами находятся три ссылки: наше самое последнее видео — слева, "
                "квадратная ссылка на наш канал — в центре и целый плейлист с такими же увлекательными историями — справа. "
                "Поставьте лайк этому видео, поделитесь своим мнением в комментариях, а затем посмотрите и другие наши видео. Спасибо!")
        words = self._words(story + cta1 + middle + cta2)
        spans = sm.cta_spans(words)
        self.assertEqual(len(spans), 2, spans)
        # кусок со второй половиной первого призыва — без слова «подпишитесь»
        second_half = [w for w in words if w.word.startswith("колокольчик")][0]
        beat = sm.Beat(second_half.start - 1.6, second_half.start + 2.4, "наш канал, также включив колокольчик уведомлений.")
        self.assertTrue(sm.in_cta(beat, spans))
        story_beat = sm.Beat(words[1].start, words[6].end, story)
        self.assertFalse(sm.in_cta(story_beat, spans), "обычный текст — кадр под слова")
        tail = [w for w in words if w.word.startswith("квадратная")][0]
        self.assertTrue(sm.in_cta(sm.Beat(tail.start, tail.start + 4, "квадратная ссылка на наш канал"), spans))

    def test_story_mentions_are_not_cta(self) -> None:
        from engine.slides import strict_media as sm

        words = self._words("Он подписал договор аренды и уехал в другой город. Канал вдоль улицы зарос травой.")
        self.assertEqual(sm.cta_spans(words), [])

    def test_beats_in_cta_get_theme_video(self) -> None:
        import json as _json
        from unittest import mock as _mock

        from engine.slides import strict_media as sm
        from engine.slides.theme_plan import ThemePlan

        plan = ThemePlan(summary="Pakistan today", video_queries=["Lahore old city aerial cinematic"], image_queries=["Lahore"])
        beats = [sm.Beat(300, 305, "Река разливалась."), sm.Beat(305, 310, "наш канал, также включив колокольчик уведомлений.")]
        client = _mock.Mock()
        client.complete.return_value = _json.dumps({"queries": ["Indus river flood aerial", "bell notification icon"]})
        q = sm._plan_queries(client, plan, beats, None, all_beats=True, cta=[(304.0, 312.0)])
        self.assertEqual(q[0], "Indus river flood aerial")
        self.assertTrue(q[1].startswith(sm.CTA_MARK), q[1])
        self.assertIn("Lahore", q[1], "на призыве — общий кадр по теме ролика")



if __name__ == "__main__":
    unittest.main()
