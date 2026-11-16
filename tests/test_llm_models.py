"""Выбор модели LLM: самая дешёвая по прайсу, точным задачам — самая дешёвая полная, запасные по цене."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import os
import unittest
from unittest import mock

import httpx
import openai

from engine.slides import llm_client, llm_models

PRICES = {"items": [
    {"model": "gpt-6-sol", "input_credits": 400, "output_credits": 2000},
    {"model": "gpt-6-luna", "input_credits": 80, "output_credits": 400},
    {"model": "gpt-image-2", "input_credits": None, "output_credits": None, "per_call_credits": 100},
    {"model": "gpt-6-astra", "input_credits": 2000, "output_credits": 10000},
]}


class ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"STORY_VIDEO_LLM_OFFLINE": ""})
        env.start()
        self.addCleanup(env.stop)
        llm_models._memory.clear()
        self.addCleanup(llm_models._memory.clear)

    def _serve(self, handler):
        real = httpx.Client

        def client(*a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real(*a, **kw)

        p = mock.patch.object(llm_models.httpx, "Client", client)
        p.start()
        self.addCleanup(p.stop)
        w = mock.patch.object(llm_models, "_write_file", lambda *a: None)
        w.start()
        self.addCleanup(w.stop)
        r = mock.patch.object(llm_models, "_read_file", lambda *a: None)
        r.start()
        self.addCleanup(r.stop)

    def test_live_pricing_sorted_and_tiers(self) -> None:
        self._serve(lambda req: httpx.Response(200, json=PRICES) if req.url.path == "/api/pricing" else httpx.Response(404))
        self.assertEqual(llm_models.chain("https://svc.example/v1", "k", "light"), ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
        self.assertEqual(llm_models.chain("https://svc.example/v1", "k", "smart"), ["gpt-6-sol", "gpt-6-astra", "gpt-6-luna"])

    def test_no_pricing_endpoint_uses_model_list(self) -> None:
        def handler(req):
            if req.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gpt-6-astra"}, {"id": "new-model"}, {"id": "gpt-6-luna"}]})
            return httpx.Response(404)

        self._serve(handler)
        self.assertEqual(llm_models.chain("https://svc.example/v1", "k", "light"), ["gpt-6-luna", "gpt-6-astra", "new-model"])

    def test_service_down_uses_builtin_prices(self) -> None:
        self._serve(lambda req: httpx.Response(500))
        self.assertEqual(llm_models.chain("https://svc.example/v1", "k", "light")[0], "gpt-6-luna")
        self.assertEqual(llm_models.chain("https://svc.example/v1", "k", "smart")[0], "gpt-6.1-sol")

    def test_tiers_by_task(self) -> None:
        self.assertEqual(llm_models.tier_of("judge_pool"), "smart")
        self.assertEqual(llm_models.tier_of("rewrite_review"), "smart")
        self.assertEqual(llm_models.tier_of("beat_queries_32"), "light")
        self.assertEqual(llm_models.tier_of("chapters"), "light")


class ClientFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        # отказавшие модели программа помнит до перезапуска — каждый тест с чистого листа
        llm_client._rejected.clear()
        self.addCleanup(llm_client._rejected.clear)

    def _client(self, handler, model=""):
        c = llm_client.LLMClient("k", model, "https://svc.example/v1")
        c._http = httpx.Client(transport=httpx.MockTransport(handler))
        c.client = openai.OpenAI(api_key="k", base_url="https://svc.example/v1", http_client=c._http, max_retries=0)
        c._sleep = lambda sec: None
        return c

    def _ok(self, text="ok"):
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": text}}]})

    def test_long_answer_comes_as_stream(self) -> None:
        """Ответ потоком (длинная генерация не обрывается прокси): куски склеиваются, расход токенов виден."""
        import json as _json

        sent = []

        def handler(request):
            body = _json.loads(request.content)
            sent.append(body)
            chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                       "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                      for piece in ("Первый ", "кусок, ", "второй.")]
            chunks.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
            data = "".join(f"data: {_json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data.encode())

        out = self._client(handler, model="gpt-6-luna").complete(label="rewrite_part_1", system="s", user="u")
        self.assertEqual(out, "Первый кусок, второй.")
        self.assertTrue(sent[0]["stream"])

    def test_cheapest_first_and_next_on_missing_model(self) -> None:
        seen = []

        def handler(req):
            m = __import__("json").loads(req.content)["model"]
            seen.append(m)
            if m == "gpt-6-luna":
                return httpx.Response(404, json={"error": {"message": "The model gpt-6-luna does not exist"}})
            return self._ok(m)

        out = self._client(handler).complete(label="chapters", system="s", user="u")
        self.assertEqual(seen, ["gpt-6-luna", "gpt-5.6-luna"])
        self.assertEqual(out, "gpt-5.6-luna")

    def test_vision_uses_full_model_and_skips_no_image_models(self) -> None:
        seen = []

        def handler(req):
            m = __import__("json").loads(req.content)["model"]
            seen.append(m)
            if m == "gpt-6.1-sol":
                return httpx.Response(400, json={"error": {"message": "image input is not supported"}})
            return self._ok(m)

        out = self._client(handler).complete_vision(label="judge", system="s", user_text="u", image_b64="AAAA")
        self.assertEqual(seen, ["gpt-6.1-sol", "gpt-6-sol"])
        self.assertEqual(out, "gpt-6-sol")

    def test_overloaded_goes_to_next_by_price(self) -> None:
        seen = []

        def handler(req):
            m = __import__("json").loads(req.content)["model"]
            seen.append(m)
            if len(seen) == 1:
                return httpx.Response(503, json={"error": {"message": "no upstream accounts"}})
            return self._ok(m)

        out = self._client(handler).complete(label="beat_queries_0", system="s", user="u")
        self.assertEqual(seen, ["gpt-6-luna", "gpt-5.6-luna"])
        self.assertEqual(out, "gpt-5.6-luna")

    def test_explicit_model_from_settings_first(self) -> None:
        seen = []

        def handler(req):
            seen.append(__import__("json").loads(req.content)["model"])
            return self._ok()

        self._client(handler, model="gpt-6-astra").complete(label="chapters", system="s", user="u")
        self.assertEqual(seen, ["gpt-6-astra"])


if __name__ == "__main__":
    unittest.main()


class ReasoningTests(ClientFallbackTests):
    def test_light_tasks_ask_low_reasoning_smart_do_not(self) -> None:
        bodies = []

        def handler(req):
            bodies.append(__import__("json").loads(req.content))
            return self._ok()

        c = self._client(handler)
        c.complete(label="chapters", system="s", user="u")
        c.complete(label="rewrite_review", system="s", user="u")
        c.complete_vision(label="judge", system="s", user_text="u", image_b64="AAAA")
        c.complete(label="rewrite_plan", system="s", user="u")
        self.assertEqual(bodies[0].get("reasoning_effort"), "low")
        self.assertEqual(bodies[1].get("reasoning_effort"), "low", "проверка рерайта: точная модель, короткие размышления")
        self.assertNotIn("reasoning_effort", bodies[2])
        self.assertNotIn("reasoning_effort", bodies[3], "план рерайта думает в полную силу")

    def test_unsupported_reasoning_param_dropped(self) -> None:
        bodies = []

        def handler(req):
            body = __import__("json").loads(req.content)
            bodies.append(body)
            if "reasoning_effort" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: reasoning_effort"}})
            return self._ok("fine")

        c = self._client(handler)
        self.assertEqual(c.complete(label="chapters", system="s", user="u"), "fine")
        self.assertEqual(len(bodies), 2)
        c.complete(label="chapters", system="s", user="u")
        self.assertNotIn("reasoning_effort", bodies[-1], "в этом клиенте больше не шлём")
