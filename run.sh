#!/bin/zsh
# Story Video — запуск на macOS (Apple Silicon). Первый раз сам ставит всё нужное:
#   Homebrew → Python, ffmpeg-full → библиотеки Python → Chromium → модели Whisper/CLIP.
#
#   ./run.sh               открыть приложение (окно на весь экран)
#   ./run.sh --browser     то же в браузере
#   ./run.sh --setup-only  только установить/проверить всё, не запускать
set -euo pipefail
cd "$(dirname "$0")"

export TK_SILENCE_DEPRECATION=1
export HOMEBREW_NO_ENV_HINTS=1

step() { print -P "%F{cyan}▶ $*%f"; }
warn() { print -P -u2 "%F{yellow}⚠ $*%f"; }
die()  { print -P -u2 "%F{red}✗ $*%f"; exit 1; }

SETUP_ONLY=0
APP_ARGS=()
for a in "$@"; do
  if [[ "$a" == "--setup-only" ]]; then SETUP_ONLY=1; else APP_ARGS+=("$a"); fi
done

# --- 0. Система ---
[[ "$(uname -s)" == "Darwin" ]] || die "Нужна macOS."
if [[ "$(uname -m)" != "arm64" ]]; then
  warn "Приложение рассчитано на Mac с M-чипом (arm64), у вас $(uname -m). Пробую дальше."
fi

# --- 1. Homebrew ---
BREW=""
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x "$b" ]] && BREW="$b" && break; done
if [[ -z "$BREW" ]]; then
  step "Нужен Homebrew (менеджер программ для macOS). Сейчас запустится его официальный установщик:"
  print   "  он попросит нажать Enter и ввести пароль от Mac — это нормально, один раз."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || die "Homebrew не установился. Установи вручную: https://brew.sh и запусти ./run.sh снова."
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x "$b" ]] && BREW="$b" && break; done
  [[ -n "$BREW" ]] || die "Homebrew не найден после установки."
fi
eval "$("$BREW" shellenv)"

brew_has() { "$BREW" list --versions "$1" >/dev/null 2>&1; }

# --- 2. Python ≥ 3.11 ---
py_ok() {
  [[ -x "$1" ]] && "$1" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" 2>/dev/null
}
find_python() {
  local c
  for c in "$(brew --prefix)/bin/python3.14" "$(brew --prefix)/bin/python3.13" "$(brew --prefix)/bin/python3.12" \
           "$(brew --prefix)/bin/python3.11" "$(command -v python3 2>/dev/null || true)"; do
    [[ -n "$c" ]] && py_ok "$c" && { print -r -- "$c"; return 0; }
  done
  return 1
}

VENV_PY=".venv/bin/python"
if ! py_ok "$VENV_PY"; then
  PY="$(find_python)" || {
    step "Ставлю Python 3.14…"
    "$BREW" install python@3.14 || die "Не удалось поставить Python."
    PY="$(find_python)" || die "Python 3.14 не найден после установки."
  }
fi

# --- 3. ffmpeg с поддержкой вшитых субтитров (ffmpeg-full) ---
FFMPEG_FULL="$(brew --prefix)/opt/ffmpeg-full/bin/ffmpeg"
if [[ ! -x "$FFMPEG_FULL" ]]; then
  step "Ставлю ffmpeg-full (сборка видео и вшитые субтитры)…"
  "$BREW" install ffmpeg-full || die "Не удалось поставить ffmpeg-full."
fi
# вывод целиком в переменную: «| grep -q» с pipefail дал бы ложную ошибку (обрыв канала)
FF_FILTERS="$("$FFMPEG_FULL" -hide_banner -filters 2>/dev/null || true)"
[[ "$FF_FILTERS" == *" subtitles "* ]] \
  || warn "В ffmpeg нет фильтра subtitles — субтитры будут дорожкой, а не вшитыми."

# --- 4. Библиотеки Python (ровно проверенные версии из requirements.lock) ---
if ! py_ok "$VENV_PY"; then
  step "Создаю окружение Python ($("$PY" -V))…"
  rm -rf .venv
  "$PY" -m venv .venv
  "$VENV_PY" -m pip install -q --upgrade pip
fi
DEPS_MARK=".venv/.deps_ok"
if [[ ! -f "$DEPS_MARK" || requirements.txt -nt "$DEPS_MARK" || requirements.lock -nt "$DEPS_MARK" ]]; then
  step "Ставлю библиотеки Python (первый раз 5–10 минут: torch, Whisper, CLIP)…"
  CONSTRAINT=()
  [[ -f requirements.lock ]] && CONSTRAINT=(-c requirements.lock)
  "$VENV_PY" -m pip install -q -r requirements.txt "${CONSTRAINT[@]}" \
    || die "Библиотеки не установились (см. ошибку выше). Проверь интернет и запусти ./run.sh снова."
  "$VENV_PY" -m pip check >/dev/null || warn "pip check: есть несовместимые версии библиотек."
  touch "$DEPS_MARK"
fi

# yt-dlp не фиксируем: YouTube часто меняется, свежая версия нужна раз в неделю
if [[ -z "$(find .venv/.ytdlp_ok -mtime -7 2>/dev/null)" ]]; then
  "$VENV_PY" -m pip install -q --upgrade yt-dlp && touch .venv/.ytdlp_ok \
    || warn "yt-dlp не обновился — нарезки с YouTube могут не скачиваться."
fi

# --- 5. Chromium для поиска картинок Google/Bing ---
if [[ ! -f .venv/.chromium_ok ]]; then
  step "Ставлю Chromium для поиска картинок…"
  if "$VENV_PY" -m playwright install chromium >/dev/null 2>&1; then
    touch .venv/.chromium_ok
  else
    warn "Chromium не поставился — поиск в Google/Bing будет недоступен, остальные источники работают."
  fi
fi

# --- 6. Модели Whisper и CLIP — заранее, а не посреди первого ролика ---
if [[ ! -f .venv/.models_ok ]]; then
  step "Скачиваю модели Whisper, CLIP и лиц (~1,5 ГБ, один раз)…"
  if "$VENV_PY" - <<'PY'
from engine.slides.whisper_timings import get_model
from engine.slides.clip_ranker import get_clip
from engine.slides.faces import ensure_models
get_model()
get_clip()
ensure_models()
PY
  then
    touch .venv/.models_ok
  else
    warn "Модели не скачались сейчас — скачаются при первом ролике."
  fi
fi

# --- 7. Помощник распознавания текста на обложках (Swift, Vision) — собрать заранее ---
if [[ ! -x data/bin/mac_ocr || tools/mac_ocr.swift -nt data/bin/mac_ocr ]]; then
  if command -v swiftc >/dev/null 2>&1 || xcode-select -p >/dev/null 2>&1; then
    step "Собираю помощник распознавания текста на обложках…"
    "$VENV_PY" -c "from engine.publish.ocr import _binary; raise SystemExit(0 if _binary() else 1)" >/dev/null 2>&1 \
      || warn "Помощник распознавания не собрался — проверка букв на обложках пойдёт через нейросеть."
  else
    warn "Нет Command Line Tools (swiftc) — проверка букв на обложках пойдёт через нейросеть. Поставить: xcode-select --install"
  fi
fi

# --- 8. Переписывание по подписке: Claude Code и Codex (ChatGPT). Вход в аккаунты — в Настройках ---
if [[ ! -x "$HOME/.local/bin/claude" ]] && ! command -v claude >/dev/null 2>&1; then
  step "Ставлю Claude Code (рерайт по подписке Claude)…"
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 \
    || warn "Claude Code не поставился — поставишь в Настройки → Переписывание → «Установить и войти»."
fi
if ! command -v codex >/dev/null 2>&1; then
  step "Ставлю Codex (рерайт по подписке ChatGPT)…"
  "$BREW" install --cask codex >/dev/null 2>&1 \
    || warn "Codex не поставился — поставишь в Настройки → Переписывание → «Установить и войти»."
  touch .venv/.codex_ok
elif [[ -z "$(find .venv/.codex_ok -mtime -7 2>/dev/null)" ]]; then
  # Codex обновляется часто (модели, вход) — раз в неделю подтягиваем свежий; Claude Code обновляется сам
  { "$BREW" upgrade --cask codex >/dev/null 2>&1 || true; } && touch .venv/.codex_ok
fi

# --- 9. Интерфейс: готовая сборка лежит в репозитории; Node нужен только при правках ---
if [[ ! -f web/dist/index.html ]] || [[ -n "$(find web/src web/index.html web/package.json -newer web/dist/index.html 2>/dev/null | head -1)" ]]; then
  if command -v npm >/dev/null 2>&1; then
    step "Пересобираю интерфейс…"
    ( cd web && { [[ -d node_modules && ! package.json -nt node_modules ]] || npm install --no-audit --no-fund; } && npm run build >/dev/null ) \
      || die "Интерфейс не собрался."
  elif [[ ! -f web/dist/index.html ]]; then
    step "Ставлю Node.js для сборки интерфейса…"
    "$BREW" install node || die "Не удалось поставить Node.js."
    ( cd web && npm install --no-audit --no-fund && npm run build >/dev/null ) || die "Интерфейс не собрался."
  else
    warn "Исходники интерфейса новее сборки, но Node.js нет — запускаю имеющуюся сборку."
  fi
fi

if [[ "$SETUP_ONLY" -eq 1 ]]; then
  step "Всё установлено. Запуск: ./run.sh"
  exit 0
fi

exec "$VENV_PY" desktop.py "${APP_ARGS[@]}"
