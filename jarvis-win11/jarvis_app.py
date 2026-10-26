from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import requests
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSpacerItem, QTextEdit, QVBoxLayout, QWidget,
)

APP_NAME = "JARVIS"
APP_VERSION = "1.1.0"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/garvard1293-arch/jarvis-updates1/main/update.json"
APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "models" / "vosk-model-small-ru-0.22"
SETTINGS_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "JarvisAssistant"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
KEYRING_SERVICE = "JarvisAssistant"

SYSTEM_PROMPT = (
    "Ты JARVIS — краткий, умный и вежливый русскоязычный голосовой помощник. "
    "Отвечай по-русски, ясно и естественно; для голосового ответа предпочитай короткие фразы. "
    "Не используй эмодзи, смайлики, Markdown, длинные списки и описание эмоций словами. "
    "Не утверждай, что выполнил действие на ПК, если приложение отдельно не подтвердило его. "
    "Никогда не проси пользователя сообщать пароль или секретный API-ключ в чат."
)


def load_settings() -> dict[str, Any]:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {"provider": "local", "local_model": "qwen3:8b", "gemini_model": "gemini-3.8-flash", "wake_enabled": True}


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_gemini_key() -> str:
    try:
        import keyring
        value = keyring.get_password(KEYRING_SERVICE, "gemini_api_key")
        if value:
            return value
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "")


def set_gemini_key(value: str) -> None:
    import keyring
    if value:
        keyring.set_password(KEYRING_SERVICE, "gemini_api_key", value)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, "gemini_api_key")
        except keyring.errors.PasswordDeleteError:
            pass


def clean_wake_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    for phrase in ("джарвис", "джарвисc", "жарвис"):
        text = text.replace(phrase, " ")
    return re.sub(r"\s+", " ", text).strip(" ,.!?-\t")


def clean_for_speech(text: str) -> str:
    """Убирает эмодзи и разметку, которые Windows SAPI может проговаривать словами."""
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)
    text = re.sub(r"```.*?```", "код опущен", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#~]+", "", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Готово."


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value or "0")
    return tuple(int(item) for item in numbers[:4]) or (0,)


class UpdateWorker(QThread):
    update_found = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            response = requests.get(UPDATE_MANIFEST_URL, timeout=8)
            response.raise_for_status()
            manifest = response.json()
            if not isinstance(manifest, dict) or not manifest.get("version"):
                raise ValueError("Файл обновления имеет неверный формат.")
            if version_tuple(str(manifest["version"])) <= version_tuple(APP_VERSION):
                self.update_found.emit({"current": True, "version": APP_VERSION})
            else:
                self.update_found.emit(manifest)
        except Exception as exc:
            self.failed.emit(f"Не удалось проверить обновления: {exc}")


class VoiceListener(QThread):
    wake_detected = Signal()
    command_recognized = Signal(str)
    status_changed = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, model_path: Path):
        super().__init__()
        self.model_path = model_path
        self._running = True
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=80)

    def stop(self):
        self._running = False
        self.wait(2500)

    def run(self):
        try:
            import sounddevice as sd
            from vosk import Model, KaldiRecognizer
            if not self.model_path.exists():
                raise FileNotFoundError(
                    "Не найдена русская модель распознавания. Запустите install.bat и дождитесь загрузки модели."
                )
            model = Model(str(self.model_path))
            recognizer = KaldiRecognizer(model, 16000)
            recognizer.SetWords(False)

            def callback(indata, frames, time_info, status):
                if status:
                    self.status_changed.emit("Проверьте микрофон")
                try:
                    self._audio.put_nowait(bytes(indata))
                except queue.Full:
                    try:
                        self._audio.get_nowait()
                        self._audio.put_nowait(bytes(indata))
                    except queue.Empty:
                        pass

            listening_for_command = False
            deadline = 0.0
            self.status_changed.emit("Слушаю слово «Джарвис» локально")
            with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1, callback=callback):
                while self._running:
                    try:
                        data = self._audio.get(timeout=0.25)
                    except queue.Empty:
                        if listening_for_command and time.monotonic() > deadline:
                            listening_for_command = False
                            self.status_changed.emit("Слушаю слово «Джарвис» локально")
                        continue
                    if recognizer.AcceptWaveform(data):
                        try:
                            text = json.loads(recognizer.Result()).get("text", "").strip()
                        except (ValueError, TypeError):
                            text = ""
                        if not text:
                            continue
                        normalized = text.lower().replace("ё", "е")
                        if listening_for_command:
                            listening_for_command = False
                            self.command_recognized.emit(text)
                            self.status_changed.emit("Слушаю слово «Джарвис» локально")
                        elif any(wake in normalized for wake in ("джарвис", "жарвис")):
                            self.wake_detected.emit()
                            remainder = clean_wake_text(text)
                            if remainder:
                                self.command_recognized.emit(remainder)
                                self.status_changed.emit("Обрабатываю голосовую команду")
                            else:
                                listening_for_command = True
                                deadline = time.monotonic() + 12
                                self.status_changed.emit("Да? Говорите команду")
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.status_changed.emit("Голосовой режим недоступен")


class ReplyWorker(QThread):
    finished_reply = Signal(str, str)
    failed = Signal(str)

    def __init__(self, text: str, provider: str, model: str, api_key: str,
                 history: list[dict[str, str]], freedom_mode: bool = False):
        super().__init__()
        self.text, self.provider, self.model, self.api_key, self.history = text, provider, model, api_key, history
        self.freedom_mode = freedom_mode

    def run(self):
        try:
            action = run_safe_command(self.text, freedom_mode=self.freedom_mode)
            if action is not None:
                self.finished_reply.emit(action, "action")
                return
            system_prompt = SYSTEM_PROMPT
            if self.freedom_mode:
                system_prompt += (
                    " Режим расширенных действий включён. Из дополнительных локальных действий доступны только: "
                    "открыть папки пользователя (Рабочий стол, Документы, Загрузки, Изображения, Видео, Музыка, папку проектов), "
                    "Paint и Ножницы. Не утверждай, что выполнил другие действия; не запускай команды или код."
                )
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self.history[-8:])
            messages.append({"role": "user", "content": self.text})
            if self.provider == "gemini":
                if not self.api_key:
                    raise RuntimeError("Добавьте Gemini API-ключ в настройках или выберите локальную модель.")
                contents = []
                for item in messages[1:]:
                    contents.append({"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]})
                payload = {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": contents,
                    "generationConfig": {"temperature": 0.45, "maxOutputTokens": 320},
                }
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
                response = requests.post(url, params={"key": self.api_key}, json=payload, timeout=60)
                response.raise_for_status()
                data = response.json()
                answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            else:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.65, "num_ctx": 8192},
                }
                response = requests.post("http://127.0.0.1:11434/api/chat", json=payload, timeout=120)
                response.raise_for_status()
                answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                answer = "Не получилось получить ответ. Попробуйте ещё раз."
            self.finished_reply.emit(answer, "chat")
        except requests.ConnectionError:
            self.failed.emit("Не удалось подключиться к модели. Проверьте интернет или запущен ли Ollama и установлен ли выбранный локальный модельный файл.")
        except requests.HTTPError as exc:
            detail = ""
            try:
                detail = exc.response.json().get("error", {}).get("message", "")
            except Exception:
                pass
            status = getattr(exc.response, "status_code", "")
            if self.api_key:
                detail = detail.replace(self.api_key, "[ключ скрыт]")
            self.failed.emit(f"Сервис вернул ошибку: {detail or f'HTTP {status}'}")
        except Exception as exc:
            message = str(exc)
            if self.api_key:
                message = message.replace(self.api_key, "[ключ скрыт]")
            self.failed.emit(message)


def run_safe_command(text: str, freedom_mode: bool = False) -> str | None:
    """Only fixed, allowlisted actions. Never evaluates model-generated shell commands."""
    t = text.lower().replace("ё", "е").strip()
    app_patterns = {
        "блокнот": (r"\b(открой|запусти|включи)\b.*\b(блокнот|notepad)\b", ["notepad.exe"]),
        "калькулятор": (r"\b(открой|запусти|включи)\b.*\b(калькулятор|калькуляторе)\b", ["calc.exe"]),
        "проводник": (r"\b(открой|запусти|включи)\b.*\bпроводник\b", ["explorer.exe"]),
        "диспетчер задач": (r"\b(открой|запусти)\b.*\b(диспетчер задач)\b", ["taskmgr.exe"]),
    }
    for label, (pattern, command) in app_patterns.items():
        if re.search(pattern, t):
            try:
                subprocess.Popen(command, shell=False)
                return f"Открываю: {label}."
            except OSError as exc:
                return f"Не удалось запустить {label}: {exc}"
    if freedom_mode:
        extra_apps = {
            "Paint": (r"\b(открой|запусти|включи)\b.*\b(paint|паинт|рисование)\b", ["mspaint.exe"]),
            "Ножницы": (r"\b(открой|запусти|включи)\b.*\b(ножницы|snipping tool)\b", ["SnippingTool.exe"]),
        }
        for label, (pattern, command) in extra_apps.items():
            if re.search(pattern, t):
                try:
                    subprocess.Popen(command, shell=False)
                    return f"Открываю: {label}."
                except OSError as exc:
                    return f"Не удалось запустить {label}: {exc}"

        folders = {
            "Рабочий стол": (r"\b(открой|покажи)\b.*\b(рабочий стол|desktop)\b", Path.home() / "Desktop"),
            "Документы": (r"\b(открой|покажи)\b.*\b(документы|documents)\b", Path.home() / "Documents"),
            "Загрузки": (r"\b(открой|покажи)\b.*\b(загрузки|downloads)\b", Path.home() / "Downloads"),
            "Изображения": (r"\b(открой|покажи)\b.*\b(изображения|картинки|pictures)\b", Path.home() / "Pictures"),
            "Видео": (r"\b(открой|покажи)\b.*\b(видео|videos)\b", Path.home() / "Videos"),
            "Музыка": (r"\b(открой|покажи)\b.*\b(музыка|music)\b", Path.home() / "Music"),
            "Проекты JARVIS": (r"\b(открой|покажи)\b.*\b(папку проектов|jarvisprojects)\b", Path.home() / "JarvisProjects"),
        }
        for label, (pattern, folder) in folders.items():
            if re.search(pattern, t):
                if not folder.is_dir():
                    return f"Папка «{label}» не найдена: {folder}"
                try:
                    subprocess.Popen(["explorer.exe", str(folder)], shell=False)
                    return f"Открываю папку: {label}."
                except OSError as exc:
                    return f"Не удалось открыть папку «{label}»: {exc}"
    if re.search(r"\b(открой|запусти)\b.*\bпапку\b", t):
        try:
            subprocess.Popen(["explorer.exe"], shell=False)
            return "Открываю Проводник."
        except OSError as exc:
            return f"Не удалось открыть Проводник: {exc}"
    if re.search(r"\b(открой|запусти)\b.*\b(браузер|интернет)\b", t):
        webbrowser.open("https://www.google.com")
        return "Открываю браузер."
    if re.search(r"\b(открой|запусти)\b.*\b(youtube|ютуб)\b", t):
        webbrowser.open("https://www.youtube.com")
        return "Открываю YouTube."
    if re.search(r"\b(открой|запусти)\b.*\b(настройки windows|параметры windows|параметры)\b", t):
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:"], shell=False)
            return "Открываю параметры Windows."
        except OSError as exc:
            return f"Не удалось открыть параметры Windows: {exc}"
    if any(phrase in t for phrase in ("помощь с командами", "что ты умеешь", "список команд")):
        return "Пока я умею открывать блокнот, калькулятор, проводник, диспетчер задач, браузер, YouTube и параметры Windows. Команды Windows ограничены безопасным списком."
    return None


class Orb(QWidget):
    def __init__(self):
        super().__init__()
        self.phase = 0
        self.setMinimumSize(220, 220)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(50)

    def _animate(self):
        self.phase = (self.phase + 2) % 360
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 28
        x = (self.width() - side) / 2
        y = (self.height() - side) / 2
        for i in range(4):
            alpha = max(8, 35 - i * 7)
            pen = QPen(QColor(57, 201, 255, alpha), 1.2)
            p.setPen(pen)
            p.drawEllipse(int(x + i * 8), int(y + i * 8), int(side - i * 16), int(side - i * 16))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 35, 58))
        p.drawEllipse(int(x + 26), int(y + 26), int(side - 52), int(side - 52))
        p.setPen(QPen(QColor(73, 211, 255, 170), 2))
        p.drawArc(int(x + 35), int(y + 35), int(side - 70), int(side - 70), self.phase * 16, 210 * 16)
        p.setPen(QColor(224, 248, 255))
        font = QFont("Segoe UI", 18, QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "J")


class MessageBubble(QFrame):
    def __init__(self, who: str, text: str):
        super().__init__()
        self.setObjectName("assistantBubble" if who == "JARVIS" else "userBubble")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel(who)
        title.setObjectName("bubbleTitle")
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setObjectName("bubbleText")
        layout.addWidget(title)
        layout.addWidget(body)
        self.setMaximumWidth(660)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)


class SettingsDialog(QDialog):
    def __init__(self, settings: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки JARVIS")
        self.setMinimumWidth(460)
        self.settings = settings.copy()
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        heading = QLabel("Настройки помощника")
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Режим работы"))
        self.provider = QComboBox()
        self.provider.addItem("Локально · Ollama", "local")
        self.provider.addItem("Облако · Gemini API", "gemini")
        index = self.provider.findData(settings.get("provider", "local"))
        self.provider.setCurrentIndex(max(0, index))
        layout.addWidget(self.provider)
        layout.addWidget(QLabel("Локальная модель Ollama"))
        self.local_model = QLineEdit(settings.get("local_model", "qwen3:8b"))
        self.local_model.setPlaceholderText("qwen3:8b")
        layout.addWidget(self.local_model)
        layout.addWidget(QLabel("Модель Gemini"))
        self.gemini_model = QLineEdit(settings.get("gemini_model", "gemini-3.8-flash"))
        layout.addWidget(self.gemini_model)
        layout.addWidget(QLabel("Gemini API-ключ (сохраняется в хранилище учётных данных Windows)"))
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Введите новый ключ; пустое поле оставит прежний")
        layout.addWidget(self.api_key)
        self.clear_key = QCheckBox("Удалить сохранённый Gemini-ключ")
        layout.addWidget(self.clear_key)
        note = QLabel("Голосовое распознавание и команды пробуждения остаются локальными. В Gemini отправляется текст только после активации. Ключ не записывается в файл настроек.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        save = QPushButton("Сохранить")
        save.setObjectName("primaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _save(self):
        self.settings["provider"] = self.provider.currentData()
        self.settings["local_model"] = self.local_model.text().strip() or "qwen3:8b"
        self.settings["gemini_model"] = self.gemini_model.text().strip() or "gemini-3.8-flash"
        try:
            if self.clear_key.isChecked():
                set_gemini_key("")
            elif self.api_key.text().strip():
                set_gemini_key(self.api_key.text().strip())
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось сохранить ключ", f"Windows не сохранила ключ в защищённом хранилище. Установите зависимости заново или используйте переменную GEMINI_API_KEY.\n\n{exc}")
            return
        try:
            save_settings(self.settings)
        except OSError as exc:
            QMessageBox.warning(self, "Ошибка настроек", str(exc))
            return
        self.accept()


class JarvisWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.history: list[dict[str, str]] = []
        self.voice: VoiceListener | None = None
        self.worker: ReplyWorker | None = None
        self.setWindowTitle("JARVIS · Голосовой помощник")
        self.resize(1120, 760)
        self.setMinimumSize(900, 640)
        self._build_ui()
        self._apply_style()
        self._add_message("JARVIS", "Здравствуйте. Я готов к работе. Скажите «Джарвис», чтобы начать разговор; команда «создай игру» откроет Студию кода. Также её можно открыть кнопкой слева.")
        if self.settings.get("wake_enabled", True):
            QTimer.singleShot(900, self.start_listening)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(16)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 20, 18, 18)
        side.setSpacing(14)
        brand = QLabel("◈  JARVIS")
        brand.setObjectName("brand")
        side.addWidget(brand)
        tagline = QLabel("ЛИЧНЫЙ AI-ПОМОЩНИК")
        tagline.setObjectName("eyebrow")
        side.addWidget(tagline)
        side.addSpacing(22)
        self.home_button = QPushButton("  ◉   Главная")
        self.home_button.setObjectName("navSelected")
        side.addWidget(self.home_button)
        self.studio_button = QPushButton("  ⌘   Студия кода и игр")
        self.studio_button.setObjectName("navButton")
        self.studio_button.clicked.connect(lambda: self.open_studio())
        side.addWidget(self.studio_button)
        self.settings_button = QPushButton("  ⚙   Настройки")
        self.settings_button.setObjectName("navButton")
        self.settings_button.clicked.connect(self.open_settings)
        side.addWidget(self.settings_button)
        self.update_button = QPushButton(f"  ↻   Проверить обновления")
        self.update_button.setObjectName("navButton")
        self.update_button.clicked.connect(self.check_for_updates)
        side.addWidget(self.update_button)
        side.addStretch(1)
        privacy_card = QFrame()
        privacy_card.setObjectName("privacyCard")
        privacy_layout = QVBoxLayout(privacy_card)
        privacy_layout.setContentsMargins(12, 12, 12, 12)
        privacy_title = QLabel("●  ПРИВАТНОСТЬ")
        privacy_title.setObjectName("privacyTitle")
        privacy_text = QLabel("Wake-word распознаётся на этом компьютере. В облако уходит только текст запроса.")
        privacy_text.setWordWrap(True)
        privacy_text.setObjectName("hint")
        privacy_layout.addWidget(privacy_title)
        privacy_layout.addWidget(privacy_text)
        side.addWidget(privacy_card)
        outer.addWidget(sidebar)

        content = QVBoxLayout()
        content.setSpacing(14)
        top = QHBoxLayout()
        top_title = QVBoxLayout()
        title = QLabel("Добрый день")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Ваш голосовой помощник для Windows")
        subtitle.setObjectName("subtitle")
        top_title.addWidget(title)
        top_title.addWidget(subtitle)
        top.addLayout(top_title)
        top.addStretch(1)
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("modeBadge")
        top.addWidget(self.mode_badge)
        content.addLayout(top)

        stage = QFrame()
        stage.setObjectName("stage")
        stage_layout = QHBoxLayout(stage)
        stage_layout.setContentsMargins(28, 12, 28, 12)
        orb = Orb()
        stage_layout.addWidget(orb, 0, Qt.AlignmentFlag.AlignVCenter)
        stage_copy = QVBoxLayout()
        stage_copy.setSpacing(9)
        self.listen_label = QLabel("ГОТОВ К РАБОТЕ")
        self.listen_label.setObjectName("eyebrow")
        self.status_label = QLabel("Скажите «Джарвис»")
        self.status_label.setObjectName("statusTitle")
        self.status_detail = QLabel("Микрофон обрабатывается локально на вашем ПК")
        self.status_detail.setObjectName("subtitle")
        stage_copy.addWidget(self.listen_label)
        stage_copy.addWidget(self.status_label)
        stage_copy.addWidget(self.status_detail)
        stage_copy.addSpacing(10)
        self.mic_button = QPushButton("■   Отключить микрофон")
        self.mic_button.setObjectName("secondaryButton")
        self.mic_button.clicked.connect(self.toggle_listening)
        stage_copy.addWidget(self.mic_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.freedom_button = QPushButton()
        self.freedom_button.setObjectName("freedomButton")
        self.freedom_button.setCheckable(True)
        self.freedom_button.setChecked(bool(self.settings.get("freedom_mode", False)))
        self.freedom_button.setToolTip(
            "Расширенный режим добавляет только открытие папок пользователя, Paint и Ножницы. "
            "Он не разрешает запуск произвольных команд или кода."
        )
        self._update_freedom_button()
        self.freedom_button.clicked.connect(self.toggle_freedom_mode)
        stage_copy.addWidget(self.freedom_button, 0, Qt.AlignmentFlag.AlignLeft)
        stage_layout.addLayout(stage_copy, 1)
        content.addWidget(stage)

        chat_header = QHBoxLayout()
        chat_label = QLabel("РАЗГОВОР")
        chat_label.setObjectName("eyebrow")
        chat_header.addWidget(chat_label)
        chat_header.addStretch(1)
        self.clear_button = QPushButton("Очистить")
        self.clear_button.setObjectName("textButton")
        self.clear_button.clicked.connect(self.clear_chat)
        chat_header.addWidget(self.clear_button)
        content.addLayout(chat_header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("chatScroll")
        self.chat_area = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_area)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(self.chat_area)
        content.addWidget(self.scroll, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Напишите сообщение или скажите: «Джарвис, открой калькулятор»")
        self.input.returnPressed.connect(self.send_text)
        input_row.addWidget(self.input, 1)
        self.send_button = QPushButton("Отправить  ➤")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self.send_text)
        input_row.addWidget(self.send_button)
        content.addLayout(input_row)
        self.command_hint = QLabel()
        self.command_hint.setObjectName("footnote")
        content.addWidget(self.command_hint)
        outer.addLayout(content, 1)
        self.update_mode_badge()
        self._update_command_hint()

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #080f1c; color: #eaf4ff; font-family: 'Segoe UI'; font-size: 14px; }
            QMainWindow { background: #080f1c; }
            #sidebar { background: #0d1727; border: 1px solid #1b2b40; border-radius: 18px; }
            #brand { color: #ecf8ff; font-size: 23px; font-weight: 700; letter-spacing: 2px; }
            #eyebrow { color: #68ccef; font-size: 10px; font-weight: 700; letter-spacing: 1.6px; }
            #navSelected, #navButton { text-align: left; padding: 12px 10px; border-radius: 10px; border: 0; font-size: 14px; }
            #navSelected { color: #dff8ff; background: #13334a; }
            #navButton { color: #95a9bf; background: transparent; }
            #navButton:hover { background: #142439; color: white; }
            #privacyCard { background: #101f30; border: 1px solid #1d3a50; border-radius: 12px; }
            #privacyTitle { color: #61d5b0; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            #hint, #subtitle { color: #91a4ba; font-size: 12px; }
            #pageTitle { font-size: 25px; font-weight: 600; }
            #stage { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #101d30, stop:1 #0d2635); border: 1px solid #1a3850; border-radius: 18px; }
            #statusTitle { font-size: 21px; font-weight: 600; color: #effaff; }
            #modeBadge { background: #102a3c; border: 1px solid #1c5269; border-radius: 12px; color: #6bd4ff; padding: 8px 12px; font-size: 11px; font-weight: 700; }
            #chatScroll { background: #0b1422; border: 1px solid #17273a; border-radius: 14px; }
            #assistantBubble { background: #101d2d; border: 1px solid #1b344a; border-radius: 13px; }
            #userBubble { background: #12334a; border: 1px solid #1c5976; border-radius: 13px; }
            #bubbleTitle { color: #65cef3; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            #bubbleText { color: #e2edf8; font-size: 13px; }
            QLineEdit { background: #101b2b; border: 1px solid #25374e; border-radius: 12px; padding: 13px 15px; color: #eff8ff; selection-background-color: #1e87ad; }
            QLineEdit:focus { border: 1px solid #4abfe8; }
            QPushButton { padding: 11px 15px; border-radius: 10px; font-weight: 600; }
            #primaryButton { background: #167da1; color: white; border: 1px solid #259cc2; }
            #primaryButton:hover { background: #2094ba; }
            #secondaryButton { background: #132b3c; color: #77d8f8; border: 1px solid #25506a; }
            #secondaryButton:hover { background: #19384d; }
            #freedomButton { background: #142235; color: #9aacc0; border: 1px solid #2a3a4e; }
            #freedomButton:hover { background: #1a2d43; color: #eff8ff; }
            #freedomButton:checked { background: #244334; color: #b9f5ce; border: 1px solid #4ea66c; }
            #textButton { color: #81a1bc; background: transparent; border: 0; padding: 5px; }
            #textButton:hover { color: #d9efff; }
            #footnote { color: #70869d; font-size: 11px; padding-left: 3px; }
            QComboBox, QTextEdit { background: #101b2b; border: 1px solid #25374e; border-radius: 9px; padding: 8px; }
            QComboBox QAbstractItemView { background: #101b2b; selection-background-color: #167da1; }
            #dialogHeading { font-size: 22px; font-weight: 650; }
        """)

    def update_mode_badge(self):
        provider = self.settings.get("provider", "local")
        self.mode_badge.setText("●  ЛОКАЛЬНО" if provider == "local" else "●  GEMINI API")
        self.status_detail.setText("Микрофон и модель работают локально" if provider == "local" else "Микрофон локальный · текст запроса отправляется в Gemini")

    def check_for_updates(self):
        if getattr(self, "update_worker", None) and self.update_worker.isRunning():
            return
        self.update_button.setText("  …   Проверяю обновления")
        self.update_worker = UpdateWorker()
        self.update_worker.update_found.connect(self.on_update_result)
        self.update_worker.failed.connect(self.on_update_error)
        self.update_worker.finished.connect(lambda: self.update_button.setText("  ↻   Проверить обновления"))
        self.update_worker.start()

    def on_update_error(self, text: str):
        QMessageBox.warning(self, "Проверка обновлений", text)

    def on_update_result(self, manifest: dict):
        if manifest.get("current"):
            QMessageBox.information(self, "Проверка обновлений", f"Установлена последняя версия JARVIS: {APP_VERSION}.")
            return
        version = str(manifest.get("version", ""))
        notes = str(manifest.get("notes", "Новые улучшения и исправления."))
        answer = QMessageBox.question(
            self,
            "Доступно обновление",
            f"Доступна версия {version}.\n\n{notes}\n\nСкачать и установить обновление?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        download_url = str(manifest.get("download_url", ""))
        sha256 = str(manifest.get("sha256", ""))
        updater = APP_DIR / "updater.py"
        if not download_url or not sha256 or not updater.exists():
            QMessageBox.warning(self, "Обновление недоступно", "В манифесте не хватает ссылки, контрольной суммы или файла обновления.")
            return
        subprocess.Popen([sys.executable, str(updater), str(APP_DIR), download_url, sha256, str(os.getpid())], cwd=str(APP_DIR))
        self.close()

    def _update_freedom_button(self):
        enabled = self.freedom_button.isChecked()
        self.freedom_button.setText("Расширенные действия: ВКЛ" if enabled else "Разрешить свободу действий")

    def _update_command_hint(self):
        base = "Доступные команды: блокнот · калькулятор · проводник · браузер · YouTube · параметры Windows"
        if self.settings.get("freedom_mode", False):
            base += " · папки пользователя · Paint · Ножницы"
        self.command_hint.setText(base)

    def toggle_freedom_mode(self, enabled: bool):
        if enabled:
            answer = QMessageBox.warning(
                self,
                "Разрешить расширенные действия?",
                "JARVIS получит только дополнительные команды из фиксированного списка: открыть папки "
                "Рабочий стол, Документы, Загрузки, Изображения, Видео, Музыка и JarvisProjects, а также Paint и Ножницы.\n\n"
                "Это НЕ разрешает запуск произвольных программ, PowerShell/команд, изменение или удаление файлов, "
                "изменение системных настроек либо запуск сгенерированного кода. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.freedom_button.blockSignals(True)
                self.freedom_button.setChecked(False)
                self.freedom_button.blockSignals(False)
                self._update_freedom_button()
                return
        previous = bool(self.settings.get("freedom_mode", False))
        self.settings["freedom_mode"] = bool(enabled)
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.settings["freedom_mode"] = previous
            self.freedom_button.blockSignals(True)
            self.freedom_button.setChecked(previous)
            self.freedom_button.blockSignals(False)
            self._update_freedom_button()
            QMessageBox.warning(self, "Не удалось сохранить режим", str(exc))
            return
        self._update_freedom_button()
        self._update_command_hint()
        state = "включён" if enabled else "выключен"
        self._add_message("JARVIS", f"Режим расширенных действий {state}. Доступны только команды из указанного списка.")

    def _add_message(self, who: str, text: str):
        bubble = MessageBubble(who, text)
        row = QHBoxLayout()
        if who == "Вы":
            row.addStretch(1)
        row.addWidget(bubble)
        if who != "Вы":
            row.addStretch(1)
        self.chat_layout.insertLayout(max(0, self.chat_layout.count() - 1), row)
        QTimer.singleShot(30, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def start_listening(self):
        if self.voice and self.voice.isRunning():
            return
        self.voice = VoiceListener(MODEL_DIR)
        self.voice.wake_detected.connect(self.on_wake)
        self.voice.command_recognized.connect(self.on_voice_command)
        self.voice.status_changed.connect(self.on_voice_status)
        self.voice.error_occurred.connect(self.on_voice_error)
        self.voice.start()
        self.mic_button.setText("■   Отключить микрофон")
        self.settings["wake_enabled"] = True
        try:
            save_settings(self.settings)
        except OSError:
            pass

    def stop_listening(self):
        if self.voice and self.voice.isRunning():
            self.voice.stop()
        self.mic_button.setText("●   Включить «Джарвис»")
        self.listen_label.setText("МИКРОФОН ВЫКЛЮЧЕН")
        self.status_label.setText("Голосовой режим на паузе")
        self.settings["wake_enabled"] = False
        try:
            save_settings(self.settings)
        except OSError:
            pass

    def toggle_listening(self):
        if self.voice and self.voice.isRunning():
            self.stop_listening()
        else:
            self.start_listening()

    def on_wake(self):
        self.listen_label.setText("АКТИВАЦИЯ")
        self.status_label.setText("Да, слушаю")

    def on_voice_status(self, text: str):
        if text == "Слушаю слово «Джарвис» локально":
            self.listen_label.setText("ГОТОВ К РАБОТЕ")
            self.status_label.setText("Скажите «Джарвис»")
        elif text == "Да? Говорите команду":
            self.listen_label.setText("СЛУШАЮ ВАС")
            self.status_label.setText("Говорите команду")
        else:
            self.listen_label.setText("ГОЛОСОВОЙ РЕЖИМ")
            self.status_label.setText(text)

    def on_voice_error(self, text: str):
        self.status_label.setText("Не удалось включить микрофон")
        self.listen_label.setText("ПРОВЕРЬТЕ НАСТРОЙКИ")
        self.status_detail.setText(text[:180])
        self.mic_button.setText("●   Повторить запуск")

    def on_voice_command(self, text: str):
        if self._open_studio_for_request(text):
            return
        self._add_message("Вы", text)
        self.status_label.setText("Обрабатываю запрос…")
        self._process(text)

    def send_text(self):
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        if self._open_studio_for_request(text):
            return
        self._add_message("Вы", text)
        self.status_label.setText("Обрабатываю запрос…")
        self._process(text)

    @staticmethod
    def _is_studio_request(text: str) -> bool:
        normalized = text.casefold()
        triggers = (
            "открой студию кода", "открой студию игр", "открой студию",
            "создай игру", "напиши игру", "сделай игру", "разработай игру",
            "создай проект игры", "напиши код для игры", "создай программу",
            "напиши программу", "сгенерируй проект", "сгенерируй код",
        )
        return any(trigger in normalized for trigger in triggers)

    def _open_studio_for_request(self, text: str) -> bool:
        if not self._is_studio_request(text):
            return False
        self._add_message("Вы", text)
        self.status_label.setText("Открываю Студию кода…")
        self.open_studio(text)
        self.status_label.setText("Готов к работе")
        return True

    def _process(self, text: str):
        if self.worker and self.worker.isRunning():
            self._add_message("JARVIS", "Подождите, я ещё отвечаю на предыдущий запрос.")
            return
        self.pending_text = text
        provider = self.settings.get("provider", "local")
        model = self.settings.get("gemini_model", "gemini-3.8-flash") if provider == "gemini" else self.settings.get("local_model", "qwen3:8b")
        self.worker = ReplyWorker(
            text, provider, model, get_gemini_key(), self.history.copy(),
            freedom_mode=bool(self.settings.get("freedom_mode", False)),
        )
        self.worker.finished_reply.connect(self.on_reply)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_reply(self, text: str, kind: str):
        self._add_message("JARVIS", text)
        self.history.extend([{"role": "user", "content": self.pending_text}, {"role": "assistant", "content": text}])
        if len(self.history) > 16:
            self.history = self.history[-16:]
        self.status_label.setText("Скажите «Джарвис»" if self.voice and self.voice.isRunning() else "Готов к работе")
        self.listen_label.setText("ГОТОВ К РАБОТЕ")
        if self.settings.get("voice_output", True):
            threading.Thread(target=self._speak, args=(text,), daemon=True).start()

    def on_error(self, text: str):
        self._add_message("JARVIS", text)
        self.status_label.setText("Проверьте подключение или настройки")
        self.listen_label.setText("ТРЕБУЕТСЯ ПРОВЕРКА")

    @staticmethod
    def _speak(text: str):
        try:
            import pyttsx3
            engine = pyttsx3.init("sapi5")
            engine.setProperty("rate", 190)
            voices = engine.getProperty("voices")
            for voice in voices:
                if "ru" in (voice.id + " " + voice.name).lower() or "russian" in voice.name.lower():
                    engine.setProperty("voice", voice.id)
                    break
            engine.say(clean_for_speech(text))
            engine.runAndWait()
            engine.stop()
        except Exception:
            pass

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.update(dialog.settings)
            self.update_mode_badge()
            self._add_message("JARVIS", "Настройки обновлены. Новые запросы будут использовать выбранный режим.")

    def open_studio(self, initial_prompt: str | None = None):
        from studio import CodeStudioDialog
        dialog = CodeStudioDialog(self.settings, self)
        if isinstance(initial_prompt, str) and not initial_prompt.casefold().startswith("открой студию"):
            dialog.prompt.setPlainText(initial_prompt)
        dialog.exec()
        self.settings.update(dialog.settings)

    def get_gemini_key(self):
        return get_gemini_key()

    def save_settings(self, settings):
        save_settings(settings)

    def clear_chat(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
            elif item.widget():
                item.widget().deleteLater()
        self.history.clear()
        self._add_message("JARVIS", "История очищена. Чем могу помочь?")

    def closeEvent(self, event):
        if self.voice and self.voice.isRunning():
            self.voice.stop()
        event.accept()


def main():
    if sys.platform != "win32":
        print("JARVIS Windows Assistant is designed for Windows 11.")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = JarvisWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
