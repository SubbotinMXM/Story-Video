"""Сервисы озвучки: поля, нормализация, миграция старых настроек Lumean."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.core.config import _normalize
from engine.core.lumean import DEFAULT_TEMPLATE_ID, DEFAULT_VOICE_ID
from engine.tts import PROVIDERS, get_provider, provider_options
from engine.tts.base import NamedList


class NamedListTests(unittest.TestCase):
    def test_selected_falls_back_to_first(self) -> None:
        nl = NamedList.parse({"items": [{"id": "a", "name": "A"}, {"id": "b"}], "selected": "zzz"})
        self.assertEqual(nl.selected_id, "a")
        self.assertEqual(nl.items[1].name, "b")  # пустое имя → id

    def test_dedup_and_garbage(self) -> None:
        nl = NamedList.parse({"items": [{"id": "a"}, {"id": "a"}, "x", {"name": "без id"}]})
        self.assertEqual([i.id for i in nl.items], ["a"])


class LumeanProviderTests(unittest.TestCase):
    def test_registry(self) -> None:
        self.assertIn("lumean", PROVIDERS)
        with self.assertRaises(ValueError):
            get_provider("nope")

    def test_defaults_when_lists_empty(self) -> None:
        p = get_provider("lumean")
        opts = p.normalize({"api_key": "k"})
        self.assertEqual(p.choice(opts, "voice"), DEFAULT_VOICE_ID)
        self.assertEqual(p.choice(opts, "template"), DEFAULT_TEMPLATE_ID)
        self.assertEqual(p.number(opts, "speed"), 1.0)

    def test_speed_clamped(self) -> None:
        p = get_provider("lumean")
        self.assertEqual(p.number({"speed": 9}, "speed"), 1.2)

    def test_check_requires_key(self) -> None:
        p = get_provider("lumean")
        with self.assertRaisesRegex(ValueError, "API key"):
            p.check(p.normalize({}))
        p.check(p.normalize({"api_key": "k"}))


class MigrationTests(unittest.TestCase):
    def test_flat_lumean_keys_migrate(self) -> None:
        cfg = _normalize(
            {
                "lumean_api_key": "key",
                "lumean_template_id": "tpl",
                "lumean_voice_id": "v2",
                "lumean_voices": [{"id": "v1", "name": "Один"}, {"id": "v2", "name": "Два"}],
                "lumean_voice_speed": 1.1,
            }
        )
        self.assertEqual(cfg["tts_provider"], "lumean")
        opts = provider_options(cfg, "lumean")
        self.assertEqual(opts["api_key"], "key")
        self.assertEqual(opts["voice"]["selected"], "v2")
        self.assertEqual(opts["template"]["items"][0]["id"], "tpl")
        self.assertAlmostEqual(opts["speed"], 1.1)
        self.assertNotIn("lumean_api_key", cfg)


class ProviderChoiceTests(unittest.TestCase):
    def test_exactly_one_known_provider_selected(self) -> None:
        self.assertEqual(_normalize({"tts_provider": "voicegen"})["tts_provider"], "voicegen")
        for bad in ("", None, "nope", 5):
            self.assertEqual(_normalize({"tts_provider": bad})["tts_provider"], "lumean")

    def test_settings_of_other_provider_kept(self) -> None:
        # переключение на VoiceGen не стирает голоса/скорость Lumean — вернёшься, всё на месте
        cfg = _normalize({"tts_provider": "voicegen", "tts": {"lumean": {"api_key": "L", "speed": 1.1}}})
        self.assertEqual(provider_options(cfg, "lumean")["api_key"], "L")


class SavePreservesUnknownKeysTests(unittest.TestCase):
    def test_other_version_keys_survive_save(self) -> None:
        import json
        import tempfile
        from unittest import mock

        from engine.core import config as cfg_mod

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"llm_model": "m", "future_feature": {"x": 1}}), encoding="utf-8")
            with mock.patch.object(cfg_mod, "CONFIG_PATH", path), mock.patch.object(cfg_mod, "DATA_DIR", Path(tmp)):
                cfg_mod.save_config({"subtitles": True})
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["future_feature"], {"x": 1})
            self.assertTrue(raw["subtitles"])


if __name__ == "__main__":
    unittest.main()
