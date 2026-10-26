from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import requests
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


ENGINES: tuple[tuple[str, str], ...] = (
    ("godot", "Godot 4 (GDScript)"),
    ("unity", "Unity (C#)"),
    ("unreal", "Unreal Engine 5 (C++)"),
    ("gamemaker", "GameMaker (GML)"),
    ("pygame", "Pygame (Python)"),
    ("roblox", "Roblox Studio (Luau)"),
)
ENGINE_GUIDES = {
    "godot": "Создай небольшой самодостаточный проект Godot 4: project.godot, сцену .tscn и нужные .gd-скрипты. Укажи main scene в project.godot.",
    "unity": "Используй структуру Unity: исходники C# клади в Assets/Scripts/. Если нужна сцена или ресурсы, включи текстовый формат и опиши в README, как добавить их в Unity Editor.",
    "unreal": "Для Unreal Engine 5 используй структуру Source/<ProjectName>/ с .h/.cpp и .Build.cs; если задача просит новый проект, создай текстовый .uproject. Не генерируй бинарные ресурсы.",
    "gamemaker": "Создавай GML-скрипты в scripts/ и инструкции, куда поместить их в GameMaker. Не выдумывай сложные бинарные ресурсы.",
    "pygame": "Создай запускаемый вручную Python-проект с main.py и requirements.txt при необходимости; не запускай код и не добавляй командные файлы.",
    "roblox": "Создавай исходники .luau и README с точным указанием, куда поместить Script/LocalScript/ModuleScript в Roblox Studio. Не включай автоматические сетевые эксплойты.",
}
MAX_FILES = 30
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

SYSTEM_INSTRUCTIONS = (
    "Ты — помощник по разработке игр. Создавай код только для обычной разработки собственных проектов, "
    "официальных модификаций и одиночных/офлайн-модификаций. Не создавай читы для мультиплеера, "
    "обходы или уклонение от античита, инъекции в процессы и средства вмешательства в чужие игры. "
    "Не создавай команды оболочки, установщики или автозапуск; можно дать краткие шаги импорта файлов через интерфейс движка. "
    "Верни строго один JSON-объект "
    'формы {"files":[{"path":"relative/path","content":"..."}],"summary":"..."}. '
    "Все пути должны быть относительными от корня проекта, с разделителем '/', без абсолютных путей и '..'. "
    "Создавай только необходимые исходники и текстовые файлы; не включай бинарные данные. "
    "Не добавляй Markdown-ограждения вокруг JSON."
)


class StudioWorker(QThread):
    """Запрашивает модель в фоне; OpenJarvis может писать только в изолированный staging."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, *, provider: str, model: str, api_key: str, engine: str,
                 project_name: str, prompt: str, backend: str = "direct",
                 staging_dir: str = ""):
        super().__init__()
        self.provider = provider
        self.backend = backend
        self.model = model
        self.api_key = api_key
        self.engine = engine
        self.project_name = project_name
        self.prompt = prompt
        self.staging_dir = staging_dir

    def run(self) -> None:
        try:
            request_text = (
                f"Целевой движок и язык: {self.engine}\n"
                f"Имя проекта: {self.project_name}\n"
                f"Рекомендации по структуре проекта: {ENGINE_GUIDES.get(self.engine, '')}\n"
                f"Задача пользователя:\n{self.prompt}\n\n"
                "Верни JSON по указанной схеме. Пути файлов должны быть безопасными относительными путями."
            )
            if self.backend == "openjarvis":
                stage = Path(self.staging_dir).resolve()
                stage.mkdir(parents=True, exist_ok=True)
                staging_root = stage.parent.resolve()
                request_text = (
                    f"Задача: {request_text}\n\n"
                    f"Создай необходимые текстовые файлы только в каталоге подготовки `{stage}`. "
                    f"Для относительных путей используй префикс `{stage.name}/` от разрешённого корня `{staging_root}`. "
                    "Используй только инструмент file_write для файлов. Не вызывай shell, Python, команды, кодовые интерпретаторы "
                    "или внешние инструменты. Не читай и не записывай ничего вне указанной папки. "
                    "Сохраняй только исходный текст, конфигурацию и README. После записи кратко перечисли созданные файлы."
                )
                response = requests.post(
                    "http://127.0.0.1:8765/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                            {"role": "user", "content": request_text},
                        ],
                        "stream": False,
                    },
                    timeout=240,
                )
                response.raise_for_status()
                result_text = str(response.json().get("choices", [{}])[0].get("message", {}).get("content", ""))
                files: list[dict[str, str]] = []
                for path in sorted(stage.rglob("*")):
                    if path.is_symlink():
                        raise ValueError("OpenJarvis создал символьную ссылку; её содержимое не импортировано.")
                    if not path.is_file():
                        continue
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError(f"Файл {path.name} из staging превышает лимит 2 МБ.")
                    content = path.read_text(encoding="utf-8")
                    files.append({"path": path.relative_to(stage).as_posix(), "content": content})
                    if len(files) > MAX_FILES:
                        raise ValueError(f"Слишком много файлов в staging: максимум {MAX_FILES}.")
                self.completed.emit({"files": files, "summary": result_text[:4000] or "Локальный агент подготовил файлы."})
                return
            if self.provider == "gemini":
                if not self.api_key:
                    raise RuntimeError("Gemini API-ключ не найден. Добавьте его в настройках JARVIS.")
                payload = {
                    "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
                    "contents": [{"role": "user", "parts": [{"text": request_text}]}],
                    "generationConfig": {
                        "temperature": 0.35,
                        "maxOutputTokens": 16000,
                        "responseMimeType": "application/json",
                    },
                }
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
                response = requests.post(url, params={"key": self.api_key}, json=payload, timeout=180)
                response.raise_for_status()
                answer = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                        {"role": "user", "content": request_text},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.35, "num_ctx": 16384},
                }
                response = requests.post("http://127.0.0.1:11434/api/chat", json=payload, timeout=180)
                response.raise_for_status()
                answer = response.json().get("message", {}).get("content", "")
            if len(answer.encode("utf-8")) > MAX_TOTAL_BYTES + 512 * 1024:
                raise ValueError("Ответ модели слишком велик для безопасного предпросмотра.")
            data = json.loads(answer)
            if not isinstance(data, dict):
                raise ValueError("Ожидался JSON-объект с файлами и описанием.")
            self.completed.emit(data)
        except requests.ConnectionError:
            if self.backend == "openjarvis":
                self.failed.emit("OpenJarvis не запущен. Сначала выполните setup_openjarvis_backend.bat, затем start_openjarvis_backend.bat.")
            else:
                self.failed.emit("Не удалось подключиться к модели. Проверьте Ollama или интернет-соединение.")
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "")
            try:
                detail = exc.response.json().get("error", {}).get("message", "")
            except Exception:
                detail = ""
            if self.api_key and detail:
                detail = detail.replace(self.api_key, "[ключ скрыт]")
            self.failed.emit(f"Ошибка сервиса модели: {detail or f'HTTP {status}'}")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.failed.emit(f"Не удалось прочитать JSON-ответ модели: {exc}")
        except Exception as exc:
            message = str(exc)
            if self.api_key:
                message = message.replace(self.api_key, "[ключ скрыт]")
            self.failed.emit(message)
        finally:
            if self.backend == "openjarvis" and self.staging_dir:
                try:
                    stage = Path(self.staging_dir).resolve(strict=False)
                    allowed_root = (
                        Path(os.environ.get("APPDATA", str(Path.home())))
                        / "JarvisAssistant"
                        / "AgentStaging"
                    ).resolve(strict=False)
                    if stage.parent == allowed_root and stage.name:
                        shutil.rmtree(stage, ignore_errors=True)
                except OSError:
                    pass


class CodeStudioDialog(QDialog):
    """Студия кода JARVIS. Генерирует только предпросмотр; запись — по отдельному подтверждению."""

    def __init__(self, settings: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle("JARVIS · Студия кода")
        self.setMinimumSize(900, 700)
        self.resize(1080, 820)
        self.settings = settings  # сохраняем конвенцию приложения: переданный словарь обновляется на месте
        self.parent_window = parent
        self.agent_staging_root = Path(os.environ.get("APPDATA", str(Path.home()))) / "JarvisAssistant" / "AgentStaging"
        self._stage_dir: Path | None = None
        self.generated_files: list[dict[str, str]] = []
        self.generated_summary = ""
        self.worker: StudioWorker | None = None
        self._building_preview = False
        self._restoring_preferences = True

        self._build_ui()
        self._apply_style()
        self._restore_preferences()
        self._restoring_preferences = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        heading = QLabel("Студия кода")
        heading.setObjectName("dialogHeading")
        root.addWidget(heading)
        subtitle = QLabel("Генерация файлов проекта с проверяемым предпросмотром")
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("Движок"))
        self.engine = QComboBox()
        for key, label in ENGINES:
            self.engine.addItem(label, key)
        self.engine.currentIndexChanged.connect(self._engine_changed)
        engine_row.addWidget(self.engine, 1)
        root.addLayout(engine_row)

        backend_row = QHBoxLayout()
        backend_row.addWidget(QLabel("Генерация кода"))
        self.backend = QComboBox()
        self.backend.addItem("Текущая модель · Ollama/Gemini", "direct")
        self.backend.addItem("OpenJarvis agent · локально, ограниченный доступ", "openjarvis")
        self.backend.currentIndexChanged.connect(self._backend_changed)
        backend_row.addWidget(self.backend, 1)
        root.addLayout(backend_row)
        backend_hint = QLabel("OpenJarvis работает только через отдельный staging-каталог; shell-команды и запуск кода отключены.")
        backend_hint.setObjectName("hint")
        backend_hint.setWordWrap(True)
        root.addWidget(backend_hint)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Название проекта"))
        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("Например, Starfall")
        name_row.addWidget(self.project_name, 1)
        root.addLayout(name_row)

        workspace_row = QHBoxLayout()
        workspace_row.addWidget(QLabel("Рабочая папка"))
        self.workspace = QLineEdit()
        self.workspace.setPlaceholderText("Выберите существующую папку проекта")
        workspace_row.addWidget(self.workspace, 1)
        self.workspace_button = QPushButton("Обзор…")
        self.workspace_button.setObjectName("secondaryButton")
        self.workspace_button.clicked.connect(self._choose_workspace)
        workspace_row.addWidget(self.workspace_button)
        root.addLayout(workspace_row)

        executable_row = QHBoxLayout()
        self.executable_label = QLabel("Путь к приложению движка (необязательно)")
        executable_row.addWidget(self.executable_label)
        self.executable_path = QLineEdit()
        self.executable_path.setReadOnly(True)
        self.executable_path.setPlaceholderText("Не выбран")
        executable_row.addWidget(self.executable_path, 1)
        self.executable_button = QPushButton("Выбрать .exe…")
        self.executable_button.setObjectName("secondaryButton")
        self.executable_button.clicked.connect(self._choose_executable)
        executable_row.addWidget(self.executable_button)
        root.addLayout(executable_row)

        root.addWidget(QLabel("Опишите, что нужно создать"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Опишите механику, сцены, интерфейс и ограничения. Не включайте секреты или API-ключи."
        )
        self.prompt.setMinimumHeight(105)
        root.addWidget(self.prompt)

        policy = QLabel(
            "Код создаётся моделью и сначала показывается для проверки. Просмотрите каждый файл и описание "
            "перед сохранением. Допускаются собственные проекты, официальные моды и одиночные/офлайн-моды; "
            "мультиплеерные читы, обход античита и инъекции в процессы запрещены."
        )
        policy.setObjectName("hint")
        policy.setWordWrap(True)
        root.addWidget(policy)

        self.summary = QLabel("Предпросмотр появится после генерации.")
        self.summary.setObjectName("summaryCard")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        splitter = QSplitter()
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabel("Файлы проекта")
        self.file_tree.setMinimumWidth(240)
        self.file_tree.currentItemChanged.connect(self._show_file)
        self.file_preview = QPlainTextEdit()
        self.file_preview.setReadOnly(True)
        self.file_preview.setPlaceholderText("Выберите файл слева для просмотра содержимого")
        splitter.addWidget(self.file_tree)
        splitter.addWidget(self.file_preview)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        self.status = QLabel("Готово")
        self.status.setObjectName("footnote")
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        self.generate_button = QPushButton("Создать предпросмотр")
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.clicked.connect(self._generate)
        buttons.addWidget(self.generate_button)
        self.save_button = QPushButton("Проверить и сохранить…")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._confirm_and_save)
        buttons.addWidget(self.save_button)
        self.open_button = QPushButton("Открыть в движке")
        self.open_button.setObjectName("secondaryButton")
        self.open_button.clicked.connect(self._open_engine)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QDialog, QWidget { background: #080f1c; color: #eaf4ff; font-family: 'Segoe UI'; font-size: 14px; }
            #dialogHeading { color: #effaff; font-size: 23px; font-weight: 650; }
            #subtitle, #hint { color: #91a4ba; font-size: 12px; }
            #footnote { color: #70869d; font-size: 11px; }
            #summaryCard { background: #101f30; border: 1px solid #1d3a50; border-radius: 12px; padding: 10px; color: #d8eafa; }
            QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox { background: #101b2b; border: 1px solid #25374e; border-radius: 9px; padding: 8px; color: #eff8ff; selection-background-color: #1e87ad; }
            QLineEdit:focus, QPlainTextEdit:focus, QTreeWidget:focus, QComboBox:focus { border: 1px solid #4abfe8; }
            QComboBox QAbstractItemView { background: #101b2b; selection-background-color: #167da1; }
            QTreeWidget::item { padding: 4px; }
            QTreeWidget::item:selected { background: #123b55; }
            QPushButton { padding: 10px 14px; border-radius: 10px; font-weight: 600; }
            #primaryButton { background: #167da1; color: white; border: 1px solid #259cc2; }
            #primaryButton:hover { background: #2094ba; }
            #primaryButton:disabled { background: #243547; color: #71849a; border-color: #30445a; }
            #secondaryButton { background: #132b3c; color: #77d8f8; border: 1px solid #25506a; }
            #secondaryButton:hover { background: #19384d; }
        """)

    def _restore_preferences(self) -> None:
        saved_engine = str(self.settings.get("studio_engine", "godot"))
        engine_index = self.engine.findData(saved_engine)
        if engine_index >= 0:
            self.engine.setCurrentIndex(engine_index)
        saved_backend = str(self.settings.get("studio_backend", "direct"))
        backend_index = self.backend.findData(saved_backend)
        if backend_index >= 0:
            self.backend.setCurrentIndex(backend_index)
        saved_workspace = str(self.settings.get("studio_workspace", "")).strip()
        if not saved_workspace:
            default_workspace = Path.home() / "JarvisProjects"
            try:
                default_workspace.mkdir(parents=True, exist_ok=True)
                saved_workspace = str(default_workspace)
            except OSError:
                pass
        self.workspace.setText(saved_workspace)
        self._refresh_executable_path()

    def _engine_changed(self, _index: int) -> None:
        self._refresh_executable_path()
        if not self._restoring_preferences:
            self._persist_preferences()

    def _backend_changed(self, _index: int) -> None:
        if not self._restoring_preferences:
            self._persist_preferences()

    def _engine_key(self) -> str:
        return str(self.engine.currentData())

    def _engine_name(self) -> str:
        return str(self.engine.currentText())

    def _engine_paths(self) -> dict[str, str]:
        paths = self.settings.get("studio_engine_paths", {})
        return dict(paths) if isinstance(paths, dict) else {}

    def _refresh_executable_path(self) -> None:
        self.executable_path.setText(self._engine_paths().get(self._engine_key(), ""))

    def _choose_workspace(self) -> None:
        current = self.workspace.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Выберите рабочую папку", current)
        if folder:
            self.workspace.setText(folder)
            self._persist_preferences()

    def _choose_executable(self) -> None:
        current = self.executable_path.text().strip()
        start = str(Path(current).parent) if current else ""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Выберите приложение движка", start, "Приложения Windows (*.exe);;Все файлы (*)"
        )
        if filename:
            paths = self._engine_paths()
            paths[self._engine_key()] = str(Path(filename).resolve())
            self.settings["studio_engine_paths"] = paths
            self._refresh_executable_path()
            self._persist_preferences()

    def _save_helper(self):
        candidate = getattr(self.parent_window, "save_settings", None)
        if callable(candidate):
            return candidate
        try:
            import jarvis_app
            candidate = getattr(jarvis_app, "save_settings", None)
            if callable(candidate):
                return candidate
        except Exception:
            pass
        return None

    def _persist_preferences(self) -> None:
        self.settings["studio_engine"] = self._engine_key()
        self.settings["studio_backend"] = str(self.backend.currentData())
        self.settings["studio_workspace"] = self.workspace.text().strip()
        # Only executable paths and workspace are persisted here; API credentials are never copied or stored.
        try:
            save = self._save_helper()
            if save:
                save(self.settings)
        except Exception as exc:
            self.status.setText(f"Не удалось сохранить настройки: {exc}")

    def _gemini_key(self) -> str:
        for source in (self.parent_window, getattr(self.parent_window, "window", lambda: None)()):
            getter = getattr(source, "get_gemini_key", None) if source is not None else None
            if callable(getter):
                try:
                    key = getter()
                    if key:
                        return str(key)
                except Exception:
                    pass
        try:
            import jarvis_app
            getter = getattr(jarvis_app, "get_gemini_key", None)
            if callable(getter):
                return str(getter() or "")
        except Exception:
            pass
        return os.environ.get("GEMINI_API_KEY", "")

    def _generate(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        project = self.project_name.text().strip()
        prompt = self.prompt.toPlainText().strip()
        if not project:
            QMessageBox.warning(self, "Нужно название", "Введите название проекта.")
            self.project_name.setFocus()
            return
        if len(project) > 100 or any(char in project for char in '<>:"/\\|?*') or project in {".", ".."}:
            QMessageBox.warning(self, "Некорректное название", "Используйте короткое имя проекта без символов пути.")
            return
        if not prompt:
            QMessageBox.warning(self, "Нужно описание", "Опишите задачу для генерации.")
            self.prompt.setFocus()
            return
        backend = str(self.backend.currentData() or "direct")
        provider = self.settings.get("provider", "local")
        provider = "gemini" if provider == "gemini" and backend == "direct" else "local"
        model = self.settings.get("gemini_model", "gemini-3.8-flash") if provider == "gemini" else self.settings.get("local_model", "qwen3:8b")
        api_key = self._gemini_key() if provider == "gemini" else ""
        self._stage_dir = None
        staging_dir = ""
        if backend == "openjarvis":
            try:
                self.agent_staging_root.mkdir(parents=True, exist_ok=True)
                self._stage_dir = self.agent_staging_root / uuid.uuid4().hex
                self._stage_dir.mkdir(parents=True, exist_ok=False)
                staging_dir = str(self._stage_dir)
            except OSError as exc:
                QMessageBox.warning(self, "Не удалось подготовить staging", str(exc))
                return
        self._persist_preferences()
        self.generate_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("Запрос к локальному OpenJarvis agent…" if backend == "openjarvis" else f"Запрос к {model}…")
        self.summary.setText("Модель готовит файлы. Ничего не записывается и не запускается на этом этапе.")
        self.worker = StudioWorker(
            provider=provider,
            model=str(model),
            api_key=api_key,
            engine=self._engine_name(),
            project_name=project,
            prompt=prompt,
            backend=backend,
            staging_dir=staging_dir,
        )
        self.worker.completed.connect(self._generation_complete)
        self.worker.failed.connect(self._generation_failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _worker_finished(self) -> None:
        self.generate_button.setEnabled(True)

    @staticmethod
    def _validate_relative_path(raw_path: Any) -> str:
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("Путь файла пуст или имеет неверный формат.")
        if "\\" in raw_path:
            raise ValueError(f"Используйте относительный путь с '/' : {raw_path!r}")
        windows_path = PureWindowsPath(raw_path)
        path = PurePosixPath(raw_path)
        if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ValueError(f"Абсолютные пути запрещены: {raw_path}")
        parts = raw_path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Небезопасный путь: {raw_path}")
        if any(":" in part for part in parts):
            raise ValueError(f"Недопустимый компонент пути: {raw_path}")
        return "/".join(parts)

    def _generation_complete(self, data: object) -> None:
        try:
            if not isinstance(data, dict):
                raise ValueError("Ответ должен быть JSON-объектом.")
            items = data.get("files")
            summary = data.get("summary", "")
            if not isinstance(items, list) or not items:
                raise ValueError("Ответ не содержит списка файлов.")
            if len(items) > MAX_FILES:
                raise ValueError(f"Слишком много файлов: максимум {MAX_FILES}.")
            if not isinstance(summary, str):
                raise ValueError("Описание проекта должно быть текстом.")
            clean: list[dict[str, str]] = []
            seen: set[str] = set()
            total = 0
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                    raise ValueError("Каждый файл должен содержать путь и текстовое содержимое.")
                rel = self._validate_relative_path(item.get("path"))
                components = [part.casefold() for part in rel.split("/")]
                filename = components[-1]
                if any(part in {".git", ".ssh", ".venv", "venv", "node_modules", "__pycache__"} for part in components):
                    raise ValueError(f"Запись в системный или зависимый каталог запрещена: {rel}")
                if filename in {".env", "id_rsa", "credentials.json", "secrets.json"} or filename.startswith(".env.") or filename.endswith((".pem", ".key")):
                    raise ValueError(f"Запись файла с возможными секретами запрещена: {rel}")
                if Path(rel).suffix.lower() in {".exe", ".dll", ".bat", ".cmd", ".ps1", ".msi", ".com", ".scr", ".vbs"}:
                    raise ValueError(f"Сохранение исполняемого файла запрещено: {rel}")
                folded = rel.casefold()
                if folded in seen:
                    raise ValueError(f"Повторяющийся путь файла: {rel}")
                seen.add(folded)
                size = len(item["content"].encode("utf-8"))
                if size > MAX_FILE_BYTES:
                    raise ValueError(f"Файл {rel} превышает лимит 2 МБ.")
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("Общий размер файлов превышает безопасный лимит 20 МБ.")
                clean.append({"path": rel, "content": item["content"]})
            self.generated_files = clean
            self.generated_summary = summary.strip()
            self._render_preview()
            self.save_button.setEnabled(True)
            self.status.setText(f"Предпросмотр готов · {len(clean)} файл(ов) · {total / 1024:.1f} КБ")
        except Exception as exc:
            self.generated_files = []
            self.generated_summary = ""
            self.save_button.setEnabled(False)
            self.summary.setText("Ответ модели не прошёл проверку безопасности.")
            self.status.setText(str(exc))
            QMessageBox.warning(self, "Некорректный ответ", str(exc))

    def _generation_failed(self, message: str) -> None:
        self.status.setText("Не удалось создать предпросмотр")
        self.summary.setText(message)
        self.generated_files = []
        self.save_button.setEnabled(False)

    def _render_preview(self) -> None:
        self._building_preview = True
        self.file_tree.clear()
        for item in self.generated_files:
            row = QTreeWidgetItem([item["path"]])
            self.file_tree.addTopLevelItem(row)
        self._building_preview = False
        self.summary.setText(self.generated_summary or "Модель не добавила описание.")
        if self.file_tree.topLevelItemCount():
            self.file_tree.setCurrentItem(self.file_tree.topLevelItem(0))
        else:
            self.file_preview.clear()

    def _show_file(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if self._building_preview or current is None:
            return
        selected = current.text(0)
        match = next((item for item in self.generated_files if item["path"] == selected), None)
        self.file_preview.setPlainText(match["content"] if match else "")

    def _workspace_root(self) -> Path:
        raw = self.workspace.text().strip()
        if not raw:
            raise ValueError("Выберите рабочую папку проекта.")
        root = Path(raw).expanduser()
        if not root.exists() or not root.is_dir():
            raise ValueError("Рабочая папка должна существовать и быть каталогом.")
        return root.resolve(strict=True)

    @staticmethod
    def _safe_destination(root: Path, relative: str) -> Path:
        rel = CodeStudioDialog._validate_relative_path(relative)
        candidate = root.joinpath(*rel.split("/"))
        current = root
        for part in rel.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Символьные ссылки запрещены в пути: {rel}")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"Путь выходит за пределы рабочей папки: {rel}")
        return candidate

    def _confirm_and_save(self) -> None:
        if not self.generated_files:
            return
        try:
            root = self._workspace_root()
            destinations = [(item, self._safe_destination(root, item["path"])) for item in self.generated_files]
            existing = [str(path.relative_to(root)) for _, path in destinations if path.exists()]
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Невозможно сохранить", str(exc))
            return
        names = "\n".join(f"• {item['path']}" for item, _ in destinations)
        overwrite_note = "\n\nБудут заменены существующие файлы:\n" + "\n".join(existing) if existing else ""
        answer = QMessageBox.question(
            self,
            "Подтверждение сохранения",
            "Код сгенерирован моделью. Перед сохранением ещё раз проверьте предпросмотр. "
            "Сохранить перечисленные файлы в выбранную папку?\n\n" + names + overwrite_note,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if existing:
            overwrite = QMessageBox.question(
                self,
                "Подтвердите перезапись",
                "Уже существуют файлы с таким именем. Перезаписать их только после этого отдельного подтверждения?\n\n"
                + "\n".join(existing),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if overwrite != QMessageBox.StandardButton.Yes:
                return
        try:
            # Повторная проверка непосредственно перед записью; никакой код/команда не запускается.
            for item, destination in destinations:
                destination = self._safe_destination(root, item["path"])
                if destination.exists() and not destination.is_file():
                    raise ValueError(f"Целевой путь не является обычным файлом: {item['path']}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._safe_destination(root, item["path"])
                with destination.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(item["content"])
            self._persist_preferences()
            self.status.setText(f"Сохранено файлов: {len(destinations)}")
            QMessageBox.information(self, "Сохранено", f"Файлы записаны в:\n{root}")
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Ошибка сохранения", f"Не все файлы удалось записать. Проверьте папку вручную.\n\n{exc}")

    def _open_engine(self) -> None:
        try:
            root = self._workspace_root()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Рабочая папка", str(exc))
            return
        executable_text = self._engine_paths().get(self._engine_key(), "")
        if not executable_text or not Path(executable_text).is_file():
            QMessageBox.information(self, "Выберите приложение", "Для открытия проекта сначала выберите файл приложения через кнопку «Выбрать .exe…».")
            self._choose_executable()
            executable_text = self._engine_paths().get(self._engine_key(), "")
        executable = Path(executable_text) if executable_text else None
        if executable is None or not executable.is_file():
            return
        args = [str(executable)]
        engine = self._engine_key()
        if engine == "godot":
            args.extend(["--editor", "--path", str(root)])
        elif engine == "unity":
            args.extend(["-projectPath", str(root)])
        elif engine == "unreal":
            project_files = sorted(root.glob("*.uproject"))
            if len(project_files) != 1:
                QMessageBox.warning(self, "Файл Unreal-проекта", "Для Unreal Engine в корне рабочей папки должен находиться ровно один файл .uproject.")
                return
            args.append(str(project_files[0].resolve()) )
        elif engine == "gamemaker":
            projects = sorted(root.glob("*.yyp"))
            args.extend(["--project", str(projects[0].resolve()) if projects else str(root)])
        elif engine == "roblox":
            project_files = sorted([*root.glob("*.rbxlx"), *root.glob("*.rbxl")])
            args.append(str(project_files[0].resolve()) if project_files else str(root))
        else:  # Pygame: открыть папку в выбранном редакторе, но не запускать Python/игровой код.
            if executable.name.lower() in {"python.exe", "pythonw.exe", "py.exe"}:
                QMessageBox.warning(self, "Небезопасное приложение", "Для Pygame выберите редактор кода, а не интерпретатор Python. Генерируемый код не запускается.")
                return
            args.append(str(root))
        answer = QMessageBox.warning(
            self,
            "Открыть проект в движке?",
            "Игровой редактор может импортировать ресурсы, компилировать или выполнять скрипты проекта. "
            "Продолжайте только после проверки исходников. JARVIS сам не запускает файлы проекта. Открыть редактор?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.Popen(args, shell=False, cwd=str(root))
            self.status.setText("Проект передан выбранному редактору. Проверьте его содержимое перед запуском.")
        except OSError as exc:
            QMessageBox.critical(self, "Не удалось открыть приложение", str(exc))

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.status.setText("Дождитесь завершения запроса модели перед закрытием окна.")
            event.ignore()
            return
        event.accept()
