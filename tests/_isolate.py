"""Импортировать первым в каждом тесте: лог тестов не попадает в data/app.log."""

import os
import tempfile

os.environ.setdefault("STORY_VIDEO_LOG", os.path.join(tempfile.gettempdir(), "story-video-tests.log"))
