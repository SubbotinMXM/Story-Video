"""Обложки по образцу (engine.publish.cover) и материалы выкладки «Стоковых слайдов»."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from engine.publish import cover as cv
from engine.publish import slides_materials as sm
from engine.slides import ai_gen

REAL_CHECK_TEXT = cv.check_text


def setUpModule() -> None:
    """Проверка надписей ходит в сеть (нейросеть) — в тестах обложек она «всё верно»; свои тесты у неё ниже."""
    p = mock.patch.object(cv, "check_text", lambda llm, image, expected, language: (True, ""))
    p.start()
    unittest.addModuleCleanup(p.stop)


def png_bytes(w: int = 2048, h: int = 1152, noise: bool = True) -> bytes:
    import os

    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)) if noise else Image.new("RGB", (w, h), "red")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeRoyal:
    """RoyalTechno: POST /v1/jobs → id, GET → succeeded, CDN → картинка."""

    def __init__(self, fail_first: int = 0) -> None:
        self.submitted: list[dict] = []
        self.fail_first = fail_first
        self.image = png_bytes(320, 180)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/jobs":
            body = json.loads(request.content)
            self.submitted.append(body)
            return httpx.Response(201, json={"id": f"job{len(self.submitted)}", "status": "queued"})
        if request.method == "GET" and request.url.path.startswith("/v1/jobs/"):
            jid = request.url.path.rsplit("/", 1)[-1]
            n = int(jid[3:])
            if n <= self.fail_first:
                return httpx.Response(200, json={"id": jid, "status": "failed", "error": {"message": "filter"}})
            return httpx.Response(200, json={"id": jid, "status": "succeeded",
                                             "output": {"type": "image", "url": f"https://cdn.x/{jid}.png"}})
        if request.url.host == "cdn.x":
            return httpx.Response(200, content=self.image)
        return httpx.Response(404)


class CoverGenerateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeRoyal()
        for p in (mock.patch.object(ai_gen, "_TRANSPORT", httpx.MockTransport(self.fake)),
                  mock.patch.object(cv, "POLL_SEC", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        self.cs = cv.CoverSettings(api_key="k", model="nano-banana-pro-2k", variants=3, llm=("k", "u", "m"))

    def test_variants_with_reference_and_resolution(self) -> None:
        got = cv.generate(self.cs, "prompt", ["data:image/jpeg;base64,AAA"], self.tmp, "вариант1", 3)
        self.assertEqual(len(got), 3)
        sent = self.fake.submitted[0]
        self.assertEqual(sent["model"], "nano-banana-pro")
        self.assertEqual(sent["input"]["resolution"], "2k")
        self.assertEqual(sent["input"]["reference_image_urls"], ["data:image/jpeg;base64,AAA"])
        self.assertEqual(sent["input"]["aspect_ratio"], "landscape")

    def test_resume_does_not_pay_twice(self) -> None:
        cv.generate(self.cs, "prompt", [], self.tmp, "вариант1", 2)
        cv.generate(self.cs, "prompt", [], self.tmp, "вариант1", 2)
        self.assertEqual(len(self.fake.submitted), 2, "готовые варианты второй раз не заказываем")

    def test_some_failed_still_returns_rest(self) -> None:
        self.fake.fail_first = 1
        got = cv.generate(self.cs, "prompt", [], self.tmp, "вариант1", 3)
        self.assertEqual(len(got), 2)

    def test_temporary_failure_retried(self) -> None:
        self.fake.fail_first = 1
        orig = self.fake.__call__

        def handler(request):
            r = orig(request)
            if request.method == "GET" and request.url.path.endswith("/job1"):
                return httpx.Response(200, json={"id": "job1", "status": "failed",
                                                 "error": {"code": "provider_error", "message": "A temporary provider error occurred. Please retry."}})
            return r

        with mock.patch.object(ai_gen, "_TRANSPORT", httpx.MockTransport(handler)), \
             mock.patch.object(cv, "TEMP_RETRY_PAUSE", 0):
            got = cv.generate(self.cs, "prompt", [], self.tmp, "вариант1", 1)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(self.fake.submitted), 2, "временный сбой — повтор")

    def test_all_failed_is_error(self) -> None:
        self.fake.fail_first = 9
        with self.assertRaises(cv.CoverError):
            cv.generate(self.cs, "prompt", [], self.tmp, "вариант1", 2)

    def test_unknown_model(self) -> None:
        self.cs.model = "veo-3.1-lite"
        with self.assertRaises(cv.CoverError):
            cv.generate(self.cs, "p", [], self.tmp, "в", 1)

    def test_no_key(self) -> None:
        self.cs.api_key = ""
        with self.assertRaises(cv.CoverError):
            cv.generate(self.cs, "p", [], self.tmp, "в", 1)


class MakeCoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cs = cv.CoverSettings(api_key="k", variants=2, llm=("k", "u", "m"))
        self.rounds = 0
        self.people: list[dict] = []

        def gen(cs, prompt, refs, work, tag, count, **kw):
            self.rounds += 1
            self.prompt, self.refs = prompt, refs
            out = []
            for n in range(1, count + 1):
                p = work / f"{tag}_{n}.png"
                p.write_bytes(png_bytes(640, 360, noise=False))
                out.append(p)
            return out

        for p in (mock.patch.object(cv, "generate", gen),
                  mock.patch.object(cv, "analyze_full", lambda llm, ref, lang, **k: {
                      "prompt": "P" * 100, "text": ["ВЬЕТНАМ", "ДЛЯ ВЗРОСЛЫХ"], "original_text": ["X"],
                      "people": self.people}),
                  mock.patch.object(cv, "concept", lambda llm, story, lang, **k: ("C" * 100, ["ТЕКСТ"]))):
            p.start()
            self.addCleanup(p.stop)

    def test_reference_flow_picks_and_saves_jpeg(self) -> None:
        with mock.patch.object(cv, "pick_best", lambda *a, **k: (1, True, "ярче")):
            res = cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertEqual(self.rounds, 1)
        self.assertEqual(res.path.name, "обложка.jpg")
        self.assertEqual(Image.open(res.path).size, (1920, 1080))
        self.assertIn('"ВЬЕТНАМ" / "ДЛЯ ВЗРОСЛЫХ"', self.prompt, "надписи — дословно в промпт генератору")
        self.assertIn("NEW, different individual", self.prompt, "люди — другие")
        self.assertTrue(self.refs and self.refs[0].startswith("data:image/jpeg;base64,"))
        self.assertTrue((self.tmp / "образец.jpg").is_file())

    def test_known_people_stay_with_other_photos(self) -> None:
        from engine.publish import cover_people

        self.people = [{"name": "Алла Пугачёва", "where": "top row, 1st"}, {"name": "Максим Галкин", "where": "center"}]
        photo = self.tmp / "pug.jpg"
        photo.write_bytes(png_bytes(300, 300, noise=False))
        found = {}

        def find(names, ref, work, **k):
            found["names"] = names
            return {"Алла Пугачёва": photo, "Максим Галкин": None}

        with mock.patch.object(cover_people, "find_photos", find), \
                mock.patch.object(cv, "pick_best", lambda *a, **k: (0, True, "ок")):
            res = cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru",
                                title="Охуевшие звёзды")
        self.assertEqual(found["names"], ["Алла Пугачёва", "Максим Галкин"])
        self.assertEqual(len(self.refs), 2, "образец + настоящее фото Пугачёвой")
        self.assertIn("REAL PEOPLE", self.prompt)
        self.assertIn("Алла Пугачёва (top row, 1st): use the face from attached image 2", self.prompt)
        self.assertIn("Максим Галкин (center): keep this same person", self.prompt)
        self.assertIn("never censor", self.prompt)
        self.assertTrue(any("Максим Галкин" in n and "не нашлось" in n for n in res.notes))

    def test_no_people_no_extra_refs(self) -> None:
        with mock.patch.object(cv, "pick_best", lambda *a, **k: (0, True, "ок")):
            cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertEqual(len(self.refs), 1)
        self.assertNotIn("Take each face", self.prompt)

    def test_text_concept_is_not_reused_for_a_real_reference(self) -> None:
        """22.11: ночью образец не скачался — придумали по тексту; доделка с настоящим образцом взяла старое описание."""
        with mock.patch.object(cv, "pick_best", lambda *a, **k: (0, True, "ок")):
            cv.make_cover(self.cs, self.tmp, reference=None, language="ru", story="текст")
            self.assertTrue(self.prompt.startswith("C"), "без образца — по тексту")
            cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertTrue(self.prompt.startswith("P"), "образец появился — разбираем его, а не берём описание по тексту")

    def test_rejected_best_means_fresh_analysis(self) -> None:
        answers = iter([(0, False, "героини слабее образца")] * 2 + [(1, True, "как образец")])
        seen = []
        real = cv.analyze_full

        def analyze(llm, ref, lang, **k):
            seen.append(k.get("context", ""))
            return real(llm, ref, lang, **k)

        with mock.patch.object(cv, "pick_best", lambda *a, **k: next(answers)), \
             mock.patch.object(cv, "analyze_full", analyze):
            res = cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertTrue(res.acceptable)
        self.assertEqual(len(seen), 2, "образец разобран заново")
        self.assertIn("ЗАБРАКОВАНА", seen[1])
        self.assertIn("героини слабее образца", seen[1])

    def test_bad_round_generates_again(self) -> None:
        answers = iter([(0, False, "кривой текст"), (2, True, "ок")])
        with mock.patch.object(cv, "pick_best", lambda *a, **k: next(answers)):
            res = cv.make_cover(self.cs, self.tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertEqual(self.rounds, 2)
        self.assertEqual(len(res.variants), 4)
        self.assertTrue(any("круг 1" in n for n in res.notes))

    def test_never_good_takes_best_with_note(self) -> None:
        with mock.patch.object(cv, "pick_best", lambda *a, **k: (0, False, "так себе")):
            res = cv.make_cover(self.cs, self.tmp, reference=None, language="en", story="story")
        self.assertEqual(self.rounds, cv.MAX_ROUNDS)
        self.assertTrue(any("идеального" in n for n in res.notes))
        self.assertEqual(self.refs, [], "без образца — без референса")

    def test_resume_keeps_prompt(self) -> None:
        with mock.patch.object(cv, "pick_best", lambda *a, **k: (0, True, "")):
            cv.make_cover(self.cs, self.tmp, reference=None, language="en", story="s")
        with mock.patch.object(cv, "concept", side_effect=AssertionError("второй раз не спрашиваем")), \
             mock.patch.object(cv, "pick_best", lambda *a, **k: (0, True, "")):
            cv.make_cover(self.cs, self.tmp, reference=None, language="en", story="s")


class CoverHelpersTest(unittest.TestCase):
    def test_youtube_size_limit(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "big.png"
        src.write_bytes(png_bytes(2752, 1536))
        out = cv.save_for_youtube(src, tmp / "o.jpg")
        self.assertLessEqual(out.stat().st_size, cv.YT_MAX_BYTES)
        self.assertEqual(Image.open(out).size, (1920, 1080))

    def test_pick_best_parses_number(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        files = []
        for n in range(3):
            p = tmp / f"v{n}.png"
            p.write_bytes(png_bytes(320, 180, noise=False))
            files.append(p)
        with mock.patch.object(cv, "_ask_image", lambda *a, **k: {"best": 3, "acceptable": True, "reason": "ok"}):
            self.assertEqual(cv.pick_best(("k", "u", "m"), None, files, ["A"], "ru"), (2, True, "ok"))
        with mock.patch.object(cv, "_ask_image", lambda *a, **k: {"best": 9}):
            self.assertEqual(cv.pick_best(("k", "u", "m"), None, files, [], "ru")[0], 0, "мусорный номер — первый")
        with mock.patch.object(cv, "_ask_image", side_effect=RuntimeError("down")):
            self.assertEqual(cv.pick_best(("k", "u", "m"), b"", files, [], "ru")[0], 0)

    def test_json_in_chatter(self) -> None:
        self.assertEqual(cv._json('Вот: {"title": "X"} — готово'), {"title": "X"})
        with self.assertRaises(cv.CoverError):
            cv._json("нет json")

    def test_no_llm_key(self) -> None:
        with self.assertRaises(cv.CoverError):
            cv._client(None)


class SlidesMaterialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / "главы.txt").write_text("ГЛАВЫ:\n0:00 Начало", encoding="utf-8")
        (self.folder / "99 процентов главы.txt").write_text("ГЛАВЫ:\n0:00 Начало 99", encoding="utf-8")
        self.files = [(str(self.folder / "ролик.mp4"), 100), (str(self.folder / "99 процентов ролик.mp4"), 99)]
        self.cs = cv.CoverSettings(api_key="k", llm=("k", "u", "m"))
        cover_path = self.folder / "обложки" / "обложка.jpg"

        def make(cs, work, *, reference, language, story, **kw):
            self.reference, self.language = reference, language
            work.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(b"x")
            return cv.CoverResult(path=cover_path, variants=[cover_path] * 3, prompt="p", text=["T"], model=cs.model)

        for p in (mock.patch.object(cv, "make_cover", make),
                  mock.patch.object(cv, "rewrite_title", lambda llm, t, lang, **k: f"Новое: {t}"),
                  mock.patch.object(cv, "title_from_story", lambda llm, s, lang, **k: "Из текста"),
                  mock.patch.object(sm, "localize_cta", lambda cta, lang, llm, cache: cta)):
            p.start()
            self.addCleanup(p.stop)

    def test_from_original(self) -> None:
        with mock.patch.object(cv, "youtube_original", lambda url: ("Вьетнам 18+", b"img", "ru")):
            mat = sm.prepare(folder=self.folder, files=self.files, source_url="https://youtu.be/x", language="fr",
                             story="текст", cover=self.cs, desc_cta="🔔 ПОДПИШИСЬ")
        self.assertEqual(mat["title"], "Новое: Вьетнам 18+")
        self.assertEqual(self.reference, b"img")
        self.assertEqual(self.language, "fr", "надписи — на языке ролика")
        d100 = mat["descriptions"][self.files[0][0]]
        d99 = mat["descriptions"][self.files[1][0]]
        self.assertTrue(d100.startswith("🔔 ПОДПИШИСЬ\n\nГЛАВЫ:"), "сначала призыв, потом главы")
        self.assertIn("Начало 99", d99, "у копии — свои тайм-коды")
        self.assertEqual(mat["description"], d100)

    def test_without_original(self) -> None:
        mat = sm.prepare(folder=self.folder, files=self.files[:1], source_url="", language="", story="Привет мир " * 50,
                         cover=self.cs, desc_cta="CTA")
        self.assertEqual(mat["title"], "Из текста")
        self.assertFalse(hasattr(self, "reference"), "нет донора — обложку по тексту НЕ придумываем (режима нет)")
        self.assertEqual(mat["thumbnail"], "")
        self.assertTrue(any("нет ролика-донора" in x for x in mat["lines"]))

    def test_cover_failure_offline_waits_instead_of_publishing_without_cover(self) -> None:
        from engine.core import netguard

        boom = mock.patch.object(cv, "make_cover", side_effect=RuntimeError("готовая картинка не скачалась"))
        orig = mock.patch.object(cv, "youtube_original", lambda url, with_image=True: ("Ориг", b"img", "ru"))
        with boom, orig, mock.patch.object(netguard, "check_now", return_value=False):
            with self.assertRaises(RuntimeError) as err:
                sm.prepare(folder=self.folder, files=self.files[:1], source_url="https://youtu.be/x", language="ru",
                           story="s", cover=self.cs, desc_cta="CTA")
        self.assertIn("нет связи", str(err.exception), "очередь распознает обрыв и подождёт связь")
        with boom, orig, mock.patch.object(netguard, "check_now", return_value=True):
            mat = sm.prepare(folder=self.folder, files=self.files[:1], source_url="https://youtu.be/x", language="ru",
                             story="s", cover=self.cs, desc_cta="CTA")
        self.assertEqual(mat["thumbnail"], "", "связь есть, а генератор упал — как раньше: без обложки, с пометкой")

    def test_donor_cover_missing_means_no_invented_cover(self) -> None:
        from engine.core import netguard

        made = []
        with mock.patch.object(cv, "youtube_original", lambda url, with_image=True: ("Ориг", None, "ru")), \
             mock.patch.object(sm.time, "sleep", lambda s: None), \
             mock.patch.object(netguard, "check_now", return_value=True), \
             mock.patch.object(cv, "make_cover", side_effect=lambda *a, **k: made.append(1)):
            mat = sm.prepare(folder=self.folder, files=self.files[:1], source_url="https://youtu.be/x", language="ru",
                             story="s", cover=self.cs, desc_cta="CTA")
        self.assertEqual(made, [], "по тексту обложку не придумываем — только перерисовка донора")
        self.assertEqual(mat["thumbnail"], "")
        self.assertTrue(any(x.startswith(sm.COVER_FAILED) and "доделка" not in x and "Доделать ролик" in x
                            for x in mat["lines"]))

    def test_original_unavailable_falls_back(self) -> None:
        with mock.patch.object(cv, "youtube_original", side_effect=RuntimeError("bot check")):
            mat = sm.prepare(folder=self.folder, files=self.files[:1], source_url="https://youtu.be/x", language="ru",
                             story="s", cover=self.cs, desc_cta="CTA")
        self.assertEqual(mat["title"], "Из текста")
        self.assertTrue(any("не открылся" in x for x in mat["lines"]))

    def test_cover_off_keeps_title_and_description(self) -> None:
        calls = []
        with mock.patch.object(cv, "youtube_original", lambda url, with_image=True: calls.append(with_image) or ("Ориг", None, "ru")), \
             mock.patch.object(cv, "make_cover", side_effect=AssertionError("обложка выключена")):
            mat = sm.prepare(folder=self.folder, files=self.files, source_url="https://youtu.be/x", language="ru",
                             story="s", cover=self.cs, desc_cta="CTA", make_cover=False)
        self.assertEqual(calls, [False], "картинку оригинала не качаем")
        self.assertEqual(mat["thumbnail"], "")
        self.assertEqual(mat["title"], "Новое: Ориг")
        self.assertTrue(mat["descriptions"][self.files[0][0]].startswith("CTA"))

    def test_model_named_in_lines(self) -> None:
        with mock.patch.object(cv, "youtube_original", lambda url: ("Вьетнам", b"img", "ru")):
            mat = sm.prepare(folder=self.folder, files=self.files[:1], source_url="https://youtu.be/x", language="ru",
                             story="s", cover=self.cs, desc_cta="CTA")
        self.assertTrue(any("Nano Banana Pro" in x for x in mat["lines"]), mat["lines"])

    def test_description_limit_and_brackets(self) -> None:
        d = sm.description("CTA <b>", "\n".join(f"{i}:00 глава номер {i}" for i in range(2000)))
        self.assertLessEqual(len(d), sm.DESC_MAX)
        self.assertNotIn("<", d)
        self.assertTrue(d.startswith("CTA"))


class ModelLabelTest(unittest.TestCase):
    def test_label(self) -> None:
        with mock.patch.object(ai_gen, "catalog", lambda **k: ai_gen.FALLBACK_MODELS):
            self.assertEqual(cv.model_label("nano-banana-pro-2k"), "Nano Banana Pro 2K")
            self.assertEqual(cv.model_label("unknown-x"), "unknown-x")

    def test_result_and_cover_json_name_model(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cs = cv.CoverSettings(api_key="k", model="seedream-4.6", variants=1, llm=("k", "u", "m"))

        def gen(cs, prompt, refs, work, tag, count, **kw):
            p = work / f"{tag}_1.png"
            p.write_bytes(png_bytes(320, 180, noise=False))
            return [p]

        with mock.patch.object(cv, "generate", gen), mock.patch.object(ai_gen, "catalog", lambda **k: ai_gen.FALLBACK_MODELS), \
             mock.patch.object(cv, "concept", lambda *a, **k: ("C" * 100, [])):
            res = cv.make_cover(cs, tmp, reference=None, language="ru", story="s")
            self.assertEqual(res.as_dict()["model_label"], "Seedream 4.6")
        info = json.loads((tmp / cv.INFO_FILE).read_text(encoding="utf-8"))
        self.assertEqual(info["model"], "seedream-4.6")
        self.assertTrue(info["generated"])


class NoThumbnailUploadTest(unittest.TestCase):
    def test_upload_job_allows_none(self) -> None:
        from engine.publish import youtube

        job = youtube.UploadJob(file=Path("a.mp4"), title="t", description="d", thumbnail=None,
                                publish_at=__import__("datetime").datetime.now(), playlist_hint=[], coauthors=[])
        self.assertIsNone(job.thumbnail)


class CoverPromptsTest(unittest.TestCase):
    def test_defaults_render_variables_and_format(self) -> None:
        from engine.publish import cover_prompts as cp

        text = cp.render(cp.active({}), "analyze", язык="французский (fr)")
        self.assertIn("на языке французский (fr)", text)
        self.assertNotIn("{язык}", text)
        self.assertIn('"original_text"', text, "формат ответа дописан программой")
        gen = cp.render(None, "generate", надписи='"A".')
        self.assertNotIn("JSON", gen, "у правил генератора формата ответа нет")
        self.assertIn(cp.GENERATE_GUARD, gen, "открытость тела как на образце — дописывается всегда")
        self.assertTrue(gen.endswith(cp.THUMB_RULES_EN), "и правила кликабельной обложки — последними")
        for role in ("analyze", "concept", "pick"):
            self.assertIn("УПРАВЛЕНИЕ ВНИМАНИЕМ", cp.render({role: "мой промпт"}, role, язык="ru", надписи="", лист=""),
                          f"правила обложки — и к своему промпту роли {role}")
        own = cp.render({"generate": "мои правила"}, "generate")
        self.assertIn(cp.GENERATE_GUARD, own, "и к своему промпту тоже")
        self.assertIn("bikini top", cp.render({"analyze": "мой разбор"}, "analyze", язык="ru"))

    def test_own_prompt_and_selection(self) -> None:
        from engine.publish import cover_prompts as cp

        cfg = {"cover_prompts": [{"id": "a", "name": "A", "role": "pick", "text": "мой выбор {лист}"},
                                 {"id": "b", "name": "B", "role": "pick", "text": "второй {лист}"}],
               "cover_prompt": {"pick": "b"}}
        act = cp.active(cfg)
        self.assertEqual(act["pick"], "второй {лист}")
        self.assertIn("Ты — профессиональный", act["analyze"], "у остальных шагов — стандартные")
        self.assertTrue(cp.render(act, "pick", лист="X").startswith("второй X"))
        cfg["cover_prompt"] = {"pick": "нет-такого"}
        self.assertEqual(cp.active(cfg)["pick"], "мой выбор {лист}", "выбранного нет — первый свой")

    def test_settings_carry_prompts_to_llm(self) -> None:
        seen = {}

        def ask(llm, label, system, user, image):
            seen["system"] = system
            return {"prompt": "P" * 100, "text": ["X"]}

        with mock.patch.object(cv, "_ask_image", ask):
            cv.analyze(("k", "u", "m"), b"img", "ru", prompts={"analyze": "СВОЙ ПРОМПТ {язык}"})
        self.assertTrue(seen["system"].startswith("СВОЙ ПРОМПТ русский (ru)"))

    def test_analyze_reads_people_and_passes_context(self) -> None:
        seen = {}

        def ask(llm, label, system, user, image):
            seen["system"], seen["user"] = system, user
            return {"prompt": "P" * 100, "text": ["ОХУЕВШИЕ"], "people": [
                {"name": "Алла Пугачёва", "where": "top"}, {"name": "алла пугачёва"}, {"name": ""}, "мусор"]}

        with mock.patch.object(cv, "_ask_image", ask):
            a = cv.analyze_full(("k", "u", "m"), b"img", "ru", context="Название: Звёзды")
        self.assertEqual(a["people"], [{"name": "Алла Пугачёва", "where": "top"}])
        self.assertEqual(a["text"], ["ОХУЕВШИЕ"])
        self.assertIn("Название: Звёзды", seen["user"])
        self.assertIn("ИЗВЕСТНЫЕ РЕАЛЬНЫЕ ЛЮДИ", seen["system"])
        self.assertIn("МАТ НА ОБЛОЖКЕ", seen["system"])

    def test_people_and_profanity_rules_apply_to_own_prompts(self) -> None:
        from engine.publish import cover_prompts as cp

        self.assertIn("ИЗВЕСТНЫЕ РЕАЛЬНЫЕ ЛЮДИ", cp.render({"analyze": "мой разбор"}, "analyze", язык="ru"))
        self.assertIn("never censor", cp.render({"generate": "my rules {надписи}"}, "generate", надписи="x"))

    def test_runner_snapshot_uses_active_prompts(self) -> None:
        from server.runners import _cover_settings

        cfg = {"royaltechno_api_key": "r", "llm_api_key": "l",
               "cover_prompts": [{"id": "x", "name": "x", "role": "title", "text": "Т {язык}"}]}
        self.assertEqual(_cover_settings(cfg).prompts["title"], "Т {язык}")


class PromptCapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cs = cv.CoverSettings(api_key="k", model="seedream-4.6", llm=("k", "u", "m"))
        cv._PROMPT_CAPS_SEEN.clear()

    def client(self, answer: str):
        c = mock.MagicMock()
        c.complete.return_value = answer
        self.calls = c.complete
        return mock.patch.object(cv, "_client", lambda llm: c)

    def test_short_prompt_untouched(self) -> None:
        with self.client("X"):
            self.assertEqual(cv.fit_prompt(self.cs, "короткий", self.tmp), "короткий")
        self.calls.assert_not_called()

    def test_long_prompt_shortened_and_cached(self) -> None:
        long = 'Text: "ВЬЕТНАМ". ' + "detail " * 500
        with self.client('Text: "ВЬЕТНАМ". short'):
            self.assertEqual(cv.fit_prompt(self.cs, long, self.tmp), 'Text: "ВЬЕТНАМ". short')
            cv.fit_prompt(self.cs, long, self.tmp)
        self.assertEqual(self.calls.call_count, 1, "второй раз — из запомненного")

    def test_llm_fails_hard_cut(self) -> None:
        long = "word " * 1000
        with mock.patch.object(cv, "_client", side_effect=cv.CoverError("нет ключа")):
            out = cv.fit_prompt(self.cs, long, self.tmp)
        self.assertLessEqual(len(out), 1500)

    def test_nano_banana_long_prompt_ok(self) -> None:
        self.cs.model = "nano-banana-pro-2k"
        with self.client("X"):
            self.assertEqual(len(cv.fit_prompt(self.cs, "a" * 5000, self.tmp)), 5000)

    def test_cap_learned_from_error(self) -> None:
        fake = FakeRoyal()
        real = fake.__call__
        state = {"n": 0}

        def handler(request):
            if request.method == "POST" and state["n"] == 0:
                state["n"] += 1
                return httpx.Response(400, json={"error": {"code": "prompt_too_long",
                                      "message": "Prompt is 1400 characters; model 'nano-banana-2' accepts at most 900."}})
            return real(request)

        self.cs.model = "nano-banana-2"
        with mock.patch.object(ai_gen, "_TRANSPORT", httpx.MockTransport(handler)), \
             mock.patch.object(cv, "POLL_SEC", 0.0), self.client("short prompt"):
            got = cv.generate(self.cs, "p " * 700, [], self.tmp, "в", 1)
        self.assertEqual(len(got), 1)
        self.assertEqual(fake.submitted[0]["input"]["prompt"], "short prompt")
        self.assertEqual(cv.prompt_cap("nano-banana-2"), 900)
        cv._PROMPT_CAPS_SEEN.clear()


class TitleTest(unittest.TestCase):
    def test_variants_and_best_pick(self) -> None:
        asked = []

        def ask(llm, label, system, user):
            asked.append((label, system, user))
            if label == "cover_title":
                return {"titles": [{"title": "Вьетнам: жизнь на воде", "keywords": ["Вьетнам"]},
                                   {"title": "ВЬЕТНАМ 18+: как живут женщины в плавучих деревнях", "keywords": ["Вьетнам"]},
                                   {"title": "Вьетнам: жизнь на воде"}]}
            return {"best": 2, "reason": "сильнее крючок"}

        notes: list[str] = []
        with mock.patch.object(cv, "_ask_text", ask):
            t = cv.rewrite_title(("k", "u", "m"), "ВЬЕТНАМ ДЛЯ ВЗРОСЛЫХ", "ru", story="Вьетнам, плавучие деревни",
                                 variants=3, notes=notes)
        self.assertEqual(t, "ВЬЕТНАМ 18+: как живут женщины в плавучих деревнях")
        self.assertIn("Напиши 3 вариант", asked[0][1])
        self.assertIn("ВЬЕТНАМ ДЛЯ ВЗРОСЛЫХ", asked[0][2], "основа — название донора")
        self.assertNotIn("плавучие деревни", asked[0][2], "текст рерайта тему не перетягивает")
        self.assertEqual(asked[1][0], "cover_title_pick")
        self.assertIn("1. Вьетнам: жизнь на воде", asked[1][2])
        self.assertNotIn("3.", asked[1][2], "повторы убраны")
        self.assertTrue(notes and "лучшее из 2" in notes[0] and "сильнее крючок" in notes[0])

    def test_single_variant_no_pick(self) -> None:
        labels = []

        def ask(llm, label, system, user):
            labels.append(label)
            return {"title": "Индонезия: баджо — жизнь посреди океана"}  # старый формат ответа тоже понимаем

        with mock.patch.object(cv, "_ask_text", ask):
            t = cv.title_from_story(("k", "u", "m"), "текст", "ru", variants=1)
        self.assertEqual(t, "Индонезия: баджо — жизнь посреди океана")
        self.assertEqual(labels, ["cover_title"])

    def test_pick_fails_takes_first(self) -> None:
        def ask(llm, label, system, user):
            if label == "cover_title_pick":
                raise RuntimeError("down")
            return {"titles": ["A вариант", "B вариант"]}

        with mock.patch.object(cv, "_ask_text", ask):
            self.assertEqual(cv.rewrite_title(("k", "u", "m"), "orig", "ru"), "A вариант")

    def test_seo_rules_in_default_prompt(self) -> None:
        from engine.publish import cover_prompts as cp

        text = cp.render(None, "title", язык="русский (ru)", вариантов="3")
        self.assertIn("ОБЫГРАТЬ ЭТО ЖЕ НАЗВАНИЕ", text, "основа — название донора")
        self.assertIn("главный ключ донора — в начале", text, "SEO")


class PromptPruneTest(unittest.TestCase):
    def test_unedited_copies_dropped_edited_kept(self) -> None:
        from engine.publish import cover_prompts as cp

        cur = {d["role"]: d for d in cp.DEFAULTS}
        kept = cp.prune([dict(cur["pick"]), dict(cur["analyze"], text="мой разбор {язык}"),
                         {"id": "c-1", "name": "Мой", "role": "title", "text": "своё"}])
        self.assertEqual([p["role"] for p in kept], ["analyze", "title"])

    def test_old_default_upgraded(self) -> None:
        from engine.publish import cover_prompts as cp

        old = ("Ты — автор кликбейтных названий для YouTube. Перепиши название чужого ролика для нашего ролика\n"
               "на ту же тему: тот же смысл и тот же крючок (интрига, провокация, обещание), но ДРУГИМИ словами —\n"
               "синонимы, перестановка, другая подача; не один в один и не близко к тексту. Если в оригинале ЗАГЛАВНЫЕ,\n"
               "эмодзи или «!» — используй так же. Язык названия: {язык}. Длина — до 90 символов.")
        cfg = {"cover_prompts": [{"id": "default-title", "name": "Стандартный", "role": "title", "text": old}]}
        self.assertIn("SEO", cp.active(cfg)["title"])
        self.assertEqual(cp.prune(cfg["cover_prompts"]), [], "старая неизменённая копия — не хранится")


class NoReferenceModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        cv.NO_REFERENCE_MODELS.clear()
        self.addCleanup(cv.NO_REFERENCE_MODELS.clear)
        self.fake = FakeRoyal()
        real = self.fake.__call__

        def handler(request):
            if request.method == "POST" and json.loads(request.content)["input"].get("reference_image_urls"):
                return httpx.Response(400, json={"error": {"code": "unsupported_combination",
                                      "message": "Model 'gpt-image-2.5' does not support reference images."}})
            return real(request)

        for p in (mock.patch.object(ai_gen, "_TRANSPORT", httpx.MockTransport(handler)),
                  mock.patch.object(cv, "POLL_SEC", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        self.cs = cv.CoverSettings(api_key="k", model="gpt-image-2.5", llm=("k", "u", "m"))

    def test_falls_back_to_text_only(self) -> None:
        got = cv.generate(self.cs, "prompt", ["data:image/jpeg;base64,AAA"], self.tmp, "в", 2)
        self.assertEqual(len(got), 2)
        self.assertIn("gpt-image-2.5", cv.NO_REFERENCE_MODELS)
        self.assertNotIn("reference_image_urls", self.fake.submitted[-1]["input"])

    def test_translation_needs_reference(self) -> None:
        with self.assertRaisesRegex(cv.CoverError, "не умеет править"):
            cv.generate(self.cs, "edit", ["data:image/jpeg;base64,AAA"], self.tmp, "п", 1, edit_only=True)


class LaneTest(unittest.TestCase):
    def test_lanes(self) -> None:
        from server.jobs import lane_of

        self.assertEqual(lane_of("cover"), "light")
        self.assertEqual(lane_of("publish"), "upload")
        self.assertEqual(lane_of("slides_full"), "main")


class SlidesPublishOptsTest(unittest.TestCase):
    def test_no_generator_key_does_not_block(self) -> None:
        """Нет ключа RoyalTechno — выкладка всё равно ставится (обложки не будет, предупредим при запуске)."""
        from server.runners import _publish_opts, royal_warning

        cfg = {"slides_publish": True, "slides_publish_channels": ["p/c"], "llm_api_key": "x",
               "royaltechno_api_key": "", "adspower_api_key": "a"}
        with self.assertRaises(ValueError) as err:
            _publish_opts(cfg, "slides")
        self.assertNotIn("RoyalTechno", str(err.exception))  # падает только на несуществующем канале
        job = mock.Mock(kind="slides_full", params={})
        self.assertIn("обложки не сгенерируются", royal_warning(cfg, [job]))

    def test_off(self) -> None:
        from server.runners import _publish_opts

        self.assertIsNone(_publish_opts({"slides_publish": False}, "slides"))


class BrollCompetitorTest(unittest.TestCase):
    """«Рассказы» → «Переделать конкурента»: обложка и название генерируются, описание — из файла."""

    def setUp(self) -> None:
        from engine.publish import registry

        registry.save({"p": registry.ProfileEntry("p", "1", "Профиль", channels=[registry.Channel("UCc", "Канал", "@c")])})
        self.tmp = Path(tempfile.mkdtemp())
        self.descs = self.tmp / "описания.txt"
        self.descs.write_text("1. Описание первое\n2. Описание второе\n", encoding="utf-8")
        self.cfg = {"broll_publish": True, "broll_publish_channels": ["p/UCc"], "broll_publish_materials": "competitor",
                    "broll_publish_descs": str(self.descs), "llm_api_key": "x", "royaltechno_api_key": "r",
                    "adspower_api_key": "a"}

    def test_only_descs_needed(self) -> None:
        from server.runners import _publish_opts

        opts = _publish_opts(self.cfg, "broll")
        self.assertEqual(opts["materials"], "competitor")
        # без ключа генератора выкладка не падает: ролик выйдет без обложки, с предупреждением
        self.assertEqual(_publish_opts({**self.cfg, "royaltechno_api_key": ""}, "broll")["materials"], "competitor")
        with self.assertRaisesRegex(ValueError, "Файл описаний"):
            _publish_opts({**self.cfg, "broll_publish_descs": ""}, "broll")
        with self.assertRaisesRegex(ValueError, "Папка обложек"):
            _publish_opts({**self.cfg, "broll_publish_materials": "files"}, "broll")

    def test_generates_title_cover_and_takes_description(self) -> None:
        from engine.publish.run import Version
        from server import runners

        folder = self.tmp / "ролик"
        folder.mkdir()
        (folder / "Тема рассказа.txt").write_text("Текст рассказа " * 50, encoding="utf-8")
        (folder / "главы.txt").write_text("0:00 Начало", encoding="utf-8")
        seen = {}

        def tc(**kw):
            seen.update(kw)
            return "ЗАГОЛОВОК!", str(folder / "обложки" / "обложка.jpg"), ["НАДПИСЬ"], ["строка"]

        req = mock.Mock(descs_file=str(self.descs))
        with mock.patch("engine.publish.slides_materials.title_and_cover", tc):
            run = runners._broll_materials(self.cfg, {"materials": "competitor", "source_url": "https://youtu.be/x"}, req)
            mat = run(Version("ru", folder, [(str(folder / "ролик.mp4"), 100)]), None)
        self.assertEqual(mat["title"], "ЗАГОЛОВОК!")
        self.assertTrue(mat["thumbnail"].endswith("обложка.jpg"))
        self.assertIn(mat["description"], ("Описание первое", "Описание второе"))
        self.assertEqual(seen["source_url"], "https://youtu.be/x")
        self.assertTrue(seen["story"].startswith("Текст рассказа"), "текст рассказа, а не главы")


class CoverFailsTest(unittest.TestCase):
    """Обложка не сгенерировалась (нет денег, нет связи) — ролик выходит без обложки, с предупреждением и
    ссылкой на оригинал; при запуске — предупреждение, если денег может не хватить."""

    def test_no_money_upload_without_cover_with_link(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cs = cv.CoverSettings(api_key="k", llm=("k", "u", "m"))
        boom = cv.CoverError("генератор не сделал ни одной обложки: RoyalTechno 402: no funds — на балансе "
                             "RoyalTechno не хватает денег")
        with mock.patch.object(cv, "youtube_original", lambda url, with_image=True: ("Оригинал", b"img", "")), \
                mock.patch.object(cv, "rewrite_title", lambda *a, **k: "НАШЕ НАЗВАНИЕ"), \
                mock.patch.object(cv, "make_cover", side_effect=boom):
            mat = sm.prepare(folder=tmp, files=[], source_url="https://youtu.be/abc", language="ru", story="s",
                             cover=cs, desc_cta="")
        self.assertEqual(mat["thumbnail"], "")
        self.assertEqual(mat["title"], "НАШЕ НАЗВАНИЕ", "название всё равно есть")
        self.assertEqual(len(mat["warnings"]), 1)
        self.assertIn("не хватает денег", mat["warnings"][0])
        self.assertIn("https://youtu.be/abc", mat["warnings"][0])
        self.assertIn("БЕЗ обложки", mat["warnings"][0])

    def test_cancel_is_not_swallowed(self) -> None:
        from engine.core.cancel import CancelledError

        with mock.patch.object(cv, "rewrite_title", lambda *a, **k: "Т"), \
                mock.patch.object(cv, "youtube_original", lambda url, with_image=True: ("Ориг", b"img", "ru")), \
                mock.patch.object(cv, "make_cover", side_effect=CancelledError("стоп")):
            with self.assertRaises(CancelledError):
                sm.title_and_cover(folder=Path(tempfile.mkdtemp()), source_url="https://youtu.be/x", language="ru",
                                   story="s", cover=cv.CoverSettings(api_key="k", llm=("k", "u", "m")))

    def test_launch_warning_when_balance_low(self) -> None:
        from server import runners

        cfg = {"royaltechno_api_key": "r", "ai_gen_enabled": True, "ai_budget_usd": 0.4, "slides_publish": True,
               "slides_publish_cover": True, "cover_model": "nano-banana-pro-2k", "cover_variants": 3}
        jobs = [mock.Mock(kind="slides_full", params={}), mock.Mock(kind="slides_full", params={})]
        from engine.slides import ai_gen

        # прайс — из документации, без запроса живого прайса RoyalTechno
        no_net = mock.patch.object(ai_gen, "catalog", lambda **k: ai_gen.FALLBACK_MODELS)
        no_net.start()
        self.addCleanup(no_net.stop)
        need, parts = runners.royal_need(cfg, jobs)
        self.assertEqual(need, 2 * 40 + 2 * 3 * 4)
        with mock.patch("engine.slides.ai_gen.balance_cents", lambda key: 83):
            self.assertIn("$0.83", runners.royal_warning(cfg, jobs))
        with mock.patch("engine.slides.ai_gen.balance_cents", lambda key: 500):
            self.assertEqual(runners.royal_warning(cfg, jobs), "")
        with mock.patch("engine.slides.ai_gen.balance_cents", lambda key: None):
            self.assertIn("Не смог проверить", runners.royal_warning(cfg, jobs))
        self.assertEqual(runners.royal_need(cfg, [mock.Mock(kind="copies", params={})])[0], 0)
        broll = {"royaltechno_api_key": "r", "broll_publish": True, "broll_publish_materials": "files"}
        self.assertEqual(runners.royal_need(broll, [mock.Mock(kind="broll", params={})])[0], 0, "обложки с компа")


if __name__ == "__main__":
    unittest.main()


class CoverTextCheckTest(unittest.TestCase):
    """Строгая проверка надписи: буквальное чтение (macOS Vision) + разбор подозрений нейросетью по фрагменту."""

    EXP = ["СЛУЧАЙНЫЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТОРИЮ"]

    def setUp(self) -> None:
        from engine.publish import ocr

        self.ocr = ocr
        self.img = Path(tempfile.mkdtemp()) / "в.png"
        self.img.write_bytes(png_bytes(640, 360, noise=False))
        self.asked = 0

    def lines(self, *texts: str) -> list:
        return [self.ocr.Line(t, (0.1, 0.1 + i * 0.3, 0.8, 0.2)) for i, t in enumerate(texts)]

    def check(self, read: list[str], arbiter: list[bool] | None = None, lang: str = "ru"):
        answers = iter(arbiter or [])

        def ask(llm, label, system, user, image):
            self.asked += 1
            self.assertEqual(label, "cover_text_arbiter")
            return {"correct": next(answers), "defect": "буква не та"}

        with mock.patch.object(self.ocr, "read", lambda image, language: self.lines(*read)), \
                mock.patch.object(cv, "_ask_image", ask):
            return REAL_CHECK_TEXT(("k", "u", "m"), self.img, self.EXP, lang)

    def test_clean_text_passes_without_llm(self) -> None:
        self.assertTrue(self.check(["СЛУЧАЙНЫЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТОРИЮ"])[0])
        self.assertEqual(self.asked, 0)

    def test_foreign_letter_is_error_without_asking(self) -> None:
        ok, why = self.check(["СЛУЧАЙНІЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТОРИЮ"], [True, True])
        self.assertFalse(ok, "«І» в русском слове — ошибка, нейросеть её «дочитывает», не спрашиваем")
        self.assertIn("СЛУЧАЙНІЕ", why)
        self.assertEqual(self.asked, 0)
        self.assertFalse(self.check(["СЛУЧАЙЊЫЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТОРИЮ"])[0])

    def test_ocr_slip_cleared_only_by_two_yes(self) -> None:
        ok, _ = self.check(["СЛУЧАЙНЫЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТРИЮ"], [True, True])
        self.assertTrue(ok, "распознавание потеряло букву, нейросеть дважды видит слово целым — годится")
        self.assertEqual(self.asked, 2)
        self.asked = 0
        ok, why = self.check(["СЛУЧАЙНЫЕ СОБЫТИЯ", "ПОВЕРНУВШИЕ ИСТРИЮ"], [True, False])
        self.assertFalse(ok, "хоть раз «не так» — ошибка")
        self.assertIn("ИСТОРИЮ", why)

    def test_glued_words_and_unread_font_not_errors(self) -> None:
        self.EXP = ["МИР АЗИИ", "ДОК-ФИЛЬМ"]
        self.assertTrue(self.check(["МИРАЗИИ", "ДОК-ФИЛЬМ"])[0], "соседние слова склеились при чтении")
        self.EXP = ["ВЬЕТНАМ", "ТОЛЬКО 18+", "ДЛЯ ВЗРОСЛЫХ"]
        self.assertTrue(self.check(["BbE4TIHAM", "11"])[0], "шрифт не прочитался — не судим")

    def test_ocr_unavailable_falls_back_to_llm(self) -> None:
        with mock.patch.object(self.ocr, "read", lambda image, language: None), \
                mock.patch.object(cv, "_ask_image", lambda *a: {"match": True, "text": ["СЛУЧАЙЊЫЕ СОБЫТИЯ"],
                                                                 "defects": ""}):
            ok, _ = REAL_CHECK_TEXT(("k", "u", "m"), self.img, self.EXP, "ru")
        self.assertFalse(ok, "запасной путь: чужая буква в прочитанном — тоже ошибка")

    def test_broken_variant_never_best(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cs = cv.CoverSettings(api_key="k", variants=3, llm=("k", "u", "m"))

        def gen(cs, prompt, refs, work, tag, count, **kw):
            out = []
            for n in range(1, count + 1):
                p = work / f"{tag}_{n}.png"
                p.write_bytes(png_bytes(640, 360, noise=False))
                out.append(p)
            return out

        picked: list[list[str]] = []

        def pick(llm, ref, variants, *a, **k):
            picked.append([v.name for v in variants])
            return 0, True, "ок"

        check = lambda llm, v, text, lang: (v.name != "вариант1_1.png", "СЛУЧАЙНІЕ")  # noqa: E731
        with mock.patch.object(cv, "generate", gen), mock.patch.object(cv, "check_text", check), \
                mock.patch.object(cv, "pick_best", pick), \
                mock.patch.object(cv, "analyze_full", lambda llm, ref, lang, **k: {
                    "prompt": "P" * 100, "text": self.EXP, "original_text": ["X"], "people": []}):
            res = cv.make_cover(cs, tmp, reference=png_bytes(800, 450, noise=False), language="ru")
        self.assertEqual(picked[0], ["вариант1_2.png", "вариант1_3.png"], "вариант с ошибкой в выбор не попадает")
        self.assertFalse(any("ошибка" in n for n in res.notes))


class RedrawRuleTest(unittest.TestCase):
    """Обложка — перерисованный оригинал: откровенность и сексуальность образца не смягчать (22.11)."""

    def test_redraw_rule_added_to_any_analyze_prompt(self) -> None:
        from engine.publish import cover_prompts as cp

        for prompts in (None, {"analyze": "мой промпт: другие люди"}):
            text = cp.render(prompts, "analyze", язык="русский")
            self.assertIn("ПЕРЕРИСОВКА ОРИГИНАЛА", text)
            self.assertIn("large full bust", text)

    def test_no_rule_asks_to_tone_down(self) -> None:
        from engine.publish import cover_prompts as cp

        for role in ("analyze", "pick", "generate"):
            text = cp.render(None, role, язык="русский", лист="", надписи="")
            self.assertNotIn("без откровенности", text, role)
            self.assertNotIn("ПЕРЕКОМПОНУЙ", text, role)
