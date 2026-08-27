"""Native PySide6 desktop admin UI for tts-server.

Independent desktop interface for managing and testing the TTS Gateway:
- Complete system health & GPU metrics dashboard
- Interactive TTS Studio / Voice playground with live audio playback
- Full API key lifecycle management with clipboard support
- Realtime job & event activity monitoring
- Usage & latency metrics visualization
- Server subprocess controller & colorized console logs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    HAS_QT_MULTIMEDIA = True
except ImportError:
    HAS_QT_MULTIMEDIA = False

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SERVER = PROJECT_ROOT / "scripts" / "run_server.ps1"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
SCOPE_HINTS = (
    "admin.full",
    "llm.generate",
    "llm.translate",
    "tts.generate",
    "tts.clone",
    "usage.read",
)


def _ollama_is_ready() -> bool:
    """Return whether the local Ollama HTTP API is responding."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _ollama_executable() -> str | None:
    """Resolve the Ollama executable on Windows and on PATH."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def ensure_ollama_started() -> str:
    """Start local Ollama when needed and wait briefly for its API."""
    if _ollama_is_ready():
        return "ollama_ready_existing"

    executable = _ollama_executable()
    if executable is None:
        return "ollama_not_installed"

    # A desktop Ollama process may still be starting. Avoid launching a second
    # server when the executable is already present but its API is not ready.
    try:
        existing = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ollama.exe"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        has_process = "ollama.exe" in existing.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        has_process = False

    if not has_process:
        try:
            subprocess.Popen(
                [executable, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=(os.name != "nt"),
            )
        except OSError as exc:
            return f"ollama_start_failed:{type(exc).__name__}"

    for _ in range(30):
        if _ollama_is_ready():
            return "ollama_ready_started"
        time.sleep(1)
    return "ollama_start_timeout"

DARK_THEME_QSS = """
QMainWindow {
    background-color: #0f111a;
    color: #e2e8f0;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    font-size: 13px;
}
QWidget {
    background-color: #0f111a;
    color: #e2e8f0;
    font-size: 13px;
}
QGroupBox {
    background-color: #171926;
    border: 1px solid #282c40;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 16px;
    padding-bottom: 10px;
    padding-left: 10px;
    padding-right: 10px;
    font-weight: bold;
    color: #cbd5e1;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 2px;
    padding: 0 6px;
    background-color: #171926;
    color: #818cf8;
}
QTabWidget::pane {
    border: 1px solid #282c40;
    border-radius: 8px;
    background-color: #141622;
    top: -1px;
}
QTabBar::tab {
    background-color: #1a1d2d;
    color: #94a3b8;
    padding: 9px 18px;
    margin-right: 3px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border: 1px solid #232738;
    border-bottom: none;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #262a42;
    color: #ffffff;
    border-color: #4f46e5;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background-color: #202436;
    color: #e2e8f0;
}
QLineEdit, QTextEdit, QSpinBox, QComboBox {
    background-color: #1c1f30;
    border: 1px solid #2d334a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f8fafc;
    selection-background-color: #4f46e5;
}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #6366f1;
    background-color: #21253a;
}
QPushButton {
    background-color: #262a40;
    color: #f1f5f9;
    border: 1px solid #3b4261;
    border-radius: 6px;
    padding: 7px 15px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #333956;
    border-color: #6366f1;
}
QPushButton:pressed {
    background-color: #1e2235;
}
QPushButton:disabled {
    background-color: #161824;
    color: #64748b;
    border-color: #222638;
}
QPushButton#primaryButton {
    background-color: #4f46e5;
    border: 1px solid #6366f1;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#primaryButton:hover {
    background-color: #4338ca;
}
QPushButton#successButton {
    background-color: #059669;
    border: 1px solid #10b981;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#successButton:hover {
    background-color: #047857;
}
QPushButton#dangerButton {
    background-color: #dc2626;
    border: 1px solid #ef4444;
    color: #ffffff;
}
QPushButton#dangerButton:hover {
    background-color: #b91c1c;
}
QTableWidget {
    background-color: #171926;
    border: 1px solid #282c40;
    border-radius: 6px;
    gridline-color: #232738;
    color: #e2e8f0;
    selection-background-color: #3730a3;
    selection-color: #ffffff;
}
QTableWidget::item {
    padding: 6px 10px;
}
QHeaderView::section {
    background-color: #1e2235;
    color: #cbd5e1;
    padding: 7px 10px;
    border: none;
    border-right: 1px solid #282c40;
    border-bottom: 1px solid #282c40;
    font-weight: bold;
}
QProgressBar {
    background-color: #1c1f30;
    border: 1px solid #2d334a;
    border-radius: 5px;
    text-align: center;
    color: #ffffff;
    font-weight: bold;
    height: 18px;
}
QProgressBar::chunk {
    background-color: #4f46e5;
    border-radius: 4px;
}
QScrollBar:vertical {
    background-color: #141622;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #2b3048;
    min-height: 20px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background-color: #3f4669;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QCheckBox {
    spacing: 7px;
    color: #cbd5e1;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #3b4261;
    background-color: #1c1f30;
}
QCheckBox::indicator:checked {
    background-color: #4f46e5;
    border-color: #6366f1;
}
QStatusBar {
    background-color: #121420;
    color: #94a3b8;
    border-top: 1px solid #222638;
}
"""


def request_http(
    target: str,
    path: str,
    method: str = "GET",
    api_key: str = "",
    body: Any = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    base = target.strip().rstrip("/")
    target_parts = urllib.parse.urlsplit(base)
    if target_parts.scheme not in {"http", "https"} or not target_parts.netloc:
        raise ValueError("Server URL phải là http(s) URL hợp lệ")
    parts = urllib.parse.urlsplit(path)
    url = base + (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    headers = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    data = None
    if body is not None and method.upper() != "GET":
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            resp_headers = {k: v for k, v in response.headers.items()}
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        resp_headers = {k: v for k, v in exc.headers.items()}
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectionError(str(exc)) from exc
    return status, resp_headers, raw


def request_json(
    target: str,
    path: str,
    method: str = "GET",
    api_key: str = "",
    body: Any = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = request_http(target, path, method, api_key, body, timeout)
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = {"raw": raw.decode("utf-8", errors="replace")[:4000]}
    return status, value if isinstance(value, dict) else {"value": value}


class TaskSignals(QObject):
    done = Signal(object)


class ApiTask(QRunnable):
    def __init__(
        self,
        target: str,
        path: str,
        method: str,
        key: str,
        body: Any,
        callback: Callable[[Any], None],
        error: Callable[[str], None],
        timeout: float = 60.0,
        binary: bool = False,
    ) -> None:
        super().__init__()
        self.target = target
        self.path = path
        self.method = method
        self.key = key
        self.body = body
        self.callback = callback
        self.error = error
        self.timeout = timeout
        self.binary = binary
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            if self.binary:
                status, headers, raw = request_http(
                    self.target, self.path, self.method, self.key, self.body, self.timeout
                )
                if status >= 400:
                    try:
                        err_payload = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        err_payload = {"error": raw.decode("utf-8", errors="replace")[:1000]}
                    self.signals.done.emit((status, err_payload, None, self.callback, self.error, self))
                else:
                    self.signals.done.emit((status, {"data": raw, "headers": headers}, None, self.callback, self.error, self))
            else:
                status, payload = request_json(
                    self.target, self.path, self.method, self.key, self.body, self.timeout
                )
                self.signals.done.emit((status, payload, None, self.callback, self.error, self))
        except Exception as exc:
            self.signals.done.emit((0, {}, str(exc), self.callback, self.error, self))


class ProcessSignals(QObject):
    output = Signal(str)


class ServerProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.signals = ProcessSignals()
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, host: str, port: int, insecure_lan: bool) -> None:
        with self._lock:
            if self.running():
                raise RuntimeError("Server đã đang chạy")
            if not RUN_SERVER.is_file():
                raise FileNotFoundError(str(RUN_SERVER))
            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RUN_SERVER),
                "-HostAddress",
                host,
                "-PortNumber",
                str(port),
            ]
            if insecure_lan:
                command.append("-AllowInsecureLan")
            self.process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process: subprocess.Popen[str]) -> None:
        if process.stdout:
            for line in process.stdout:
                self.signals.output.emit(line.rstrip())

    def stop(self) -> None:
        with self._lock:
            process = self.process
            if process is None:
                return
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
            self.process = None


class StatCard(QFrame):
    def __init__(self, title: str, initial_value: str = "—", subtitle: str = "") -> None:
        super().__init__()
        self.setStyleSheet(
            "StatCard { background-color: #181b29; border: 1px solid #2a2f45; border-radius: 8px; padding: 10px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        
        lbl_title = QLabel(title.upper())
        lbl_title.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold; letter-spacing: 0.5px;")
        layout.addWidget(lbl_title)
        
        self.val_label = QLabel(initial_value)
        self.val_label.setStyleSheet("color: #ffffff; font-size: 20px; font-weight: bold;")
        layout.addWidget(self.val_label)
        
        self.sub_label = QLabel(subtitle)
        self.sub_label.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(self.sub_label)

    def set_value(self, text: str, color: str = "#ffffff") -> None:
        self.val_label.setText(text)
        self.val_label.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: bold;")

    def set_subtitle(self, text: str) -> None:
        self.sub_label.setText(text)


class AudioPlayer(QObject):
    """Audio playback manager supporting PySide6 QMediaPlayer or winsound fallback."""
    state_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.player = None
        self.audio_output = None
        self.current_wav_path: str | None = None
        if HAS_QT_MULTIMEDIA:
            self.player = QMediaPlayer()
            self.audio_output = QAudioOutput()
            self.player.setAudioOutput(self.audio_output)
            self.audio_output.setVolume(1.0)

    def play_bytes(self, wav_data: bytes) -> str:
        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_file.write(wav_data)
        temp_file.close()
        self.current_wav_path = temp_file.name
        self.play_file(self.current_wav_path)
        return self.current_wav_path

    def play_file(self, file_path: str) -> None:
        if self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(file_path))
            self.player.play()
            self.state_changed.emit("playing")
        elif HAS_WINSOUND:
            winsound.PlaySound(file_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self.state_changed.emit("playing")

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()
        elif HAS_WINSOUND:
            winsound.PlaySound(None, winsound.SND_PURGE)
        self.state_changed.emit("stopped")


class AdminWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TTS Server Admin & Studio")
        self.resize(1280, 860)
        self.setMinimumSize(1000, 700)
        self.pool = QThreadPool.globalInstance()
        self.server = ServerProcess()
        self.server.signals.output.connect(self.append_log)
        self.audio_player = AudioPlayer()
        self.scope_checks: dict[str, QCheckBox] = {}
        self._active_tasks: set[ApiTask] = set()
        self.cached_models: list[dict[str, Any]] = []
        self.studio_models: list[dict[str, Any]] = []
        self.last_audio_bytes: bytes | None = None
        self._realtime_inflight = False
        self._error_dialog: QMessageBox | None = None
        self._startup_probe_attempts = 0
        
        self.auto_refresh_timer = QTimer(self)
        self.auto_refresh_timer.timeout.connect(self.on_auto_refresh)
        
        self.realtime_timer = QTimer(self)
        self.realtime_timer.timeout.connect(self._poll_realtime)
        
        self.setStyleSheet(DARK_THEME_QSS)
        self._build()

    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        self.setCentralWidget(central)

        # 1. Top Header & Connection Bar
        top_bar = QFrame()
        top_bar.setStyleSheet("background-color: #171926; border: 1px solid #282c40; border-radius: 8px; padding: 6px;")
        top_layout = QGridLayout(top_bar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)

        # Connection inputs
        self.target = QLineEdit("http://127.0.0.1:8000")
        self.target.setPlaceholderText("http://127.0.0.1:8000")

        self.admin_key = QLineEdit()
        self.admin_key.setEchoMode(QLineEdit.Password)
        self.admin_key.setPlaceholderText("Admin API Key (ai_sk_...)")

        self.product_key = QLineEdit()
        self.product_key.setEchoMode(QLineEdit.Password)
        self.product_key.setPlaceholderText("Product API Key (cho TTS Studio)")

        toggle_key_btn = QPushButton("👁")
        toggle_key_btn.setToolTip("Hiện / Ẩn Key")
        toggle_key_btn.setFixedWidth(36)
        toggle_key_btn.clicked.connect(self._toggle_key_visibility)

        self.btn_connect = QPushButton("🔄 Kết nối / Tải lại")
        self.btn_connect.setObjectName("primaryButton")
        self.btn_connect.clicked.connect(self.connect)

        self.btn_toggle_server = QPushButton("▶ Bật Server")
        self.btn_toggle_server.setObjectName("successButton")
        self.btn_toggle_server.clicked.connect(self.toggle_server_process)

        self.status_dot = QLabel("● Chưa kết nối")
        self.status_dot.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 12px;")

        top_layout.addWidget(QLabel("Server URL:"), 0, 0)
        top_layout.addWidget(self.target, 0, 1)
        top_layout.addWidget(QLabel("Admin Key:"), 0, 2)
        top_layout.addWidget(self.admin_key, 0, 3)
        top_layout.addWidget(toggle_key_btn, 0, 4)
        top_layout.addWidget(self.btn_connect, 0, 5)

        top_layout.addWidget(self.status_dot, 1, 0)
        top_layout.addWidget(QLabel("Product Key:"), 1, 2)
        top_layout.addWidget(self.product_key, 1, 3)
        top_layout.addWidget(self.btn_toggle_server, 1, 5)

        top_layout.setColumnStretch(1, 2)
        top_layout.setColumnStretch(3, 3)
        root.addWidget(top_bar)

        # 2. Main Tab Widget
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self._build_dashboard_tab()
        self._build_studio_tab()
        self._build_keys_tab()
        self._build_models_tab()
        self._build_jobs_tab()
        self._build_usage_tab()
        self._build_ops_tab()

    def _toggle_key_visibility(self) -> None:
        if self.admin_key.echoMode() == QLineEdit.Password:
            self.admin_key.setEchoMode(QLineEdit.Normal)
            self.product_key.setEchoMode(QLineEdit.Normal)
        else:
            self.admin_key.setEchoMode(QLineEdit.Password)
            self.product_key.setEchoMode(QLineEdit.Password)

    # ----------------------------------------------------
    # TAB 1: DASHBOARD
    # ----------------------------------------------------
    def _build_dashboard_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Quick Stat Cards Row
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(10)
        self.card_status = StatCard("Trạng thái Server", "OFFLINE", "Chưa kết nối")
        self.card_models = StatCard("Mô hình AI", "0 / 0", "Khả dụng")
        self.card_gpu = StatCard("GPU / VRAM", "—", "Chưa phát hiện")
        self.card_scheduler = StatCard("Bộ điều phối (GPU)", "IDLE", "0 jobs chờ")
        
        cards_layout.addWidget(self.card_status)
        cards_layout.addWidget(self.card_models)
        cards_layout.addWidget(self.card_gpu)
        cards_layout.addWidget(self.card_scheduler)
        layout.addLayout(cards_layout)

        # Splitter: System Resource Gauges + Live Diagnostics
        splitter = QSplitter(Qt.Horizontal)

        # Left box: Resource Gauges
        res_box = QGroupBox("Tài nguyên phần cứng & Bộ nhớ")
        res_layout = QVBoxLayout(res_box)
        res_layout.setSpacing(10)

        # Realtime toggle row
        rt_row = QHBoxLayout()
        self.chk_realtime_dashboard = QCheckBox("⚡ Giám sát Realtime (Mỗi 2s)")
        self.chk_realtime_dashboard.setChecked(False)
        self.chk_realtime_dashboard.stateChanged.connect(self._toggle_realtime_monitor)
        rt_row.addWidget(self.chk_realtime_dashboard)
        rt_row.addStretch()
        res_layout.addLayout(rt_row)

        res_layout.addWidget(QLabel("RAM hệ thống:"))
        self.ram_bar = QProgressBar()
        self.ram_bar.setFormat("%v% (%p%)")
        self.ram_bar.setValue(0)
        res_layout.addWidget(self.ram_bar)
        self.ram_detail = QLabel("Sử dụng: — / —")
        self.ram_detail.setStyleSheet("color: #94a3b8; font-size: 11px;")
        res_layout.addWidget(self.ram_detail)

        res_layout.addWidget(QLabel("VRAM GPU (NVIDIA CUDA):"))
        self.vram_bar = QProgressBar()
        self.vram_bar.setFormat("%v% (%p%)")
        self.vram_bar.setValue(0)
        res_layout.addWidget(self.vram_bar)
        self.vram_detail = QLabel("VRAM: — / —")
        self.vram_detail.setStyleSheet("color: #94a3b8; font-size: 11px;")
        res_layout.addWidget(self.vram_detail)

        res_layout.addStretch()
        splitter.addWidget(res_box)

        # Right box: Structured Health / Details
        diag_box = QGroupBox("Chi tiết hệ thống & JSON Inspector")
        diag_layout = QVBoxLayout(diag_box)
        self.dashboard_text = QTextEdit()
        self.dashboard_text.setReadOnly(True)
        self.dashboard_text.setPlaceholderText("Kết nối server để xem cấu hình chi tiết...")
        diag_layout.addWidget(self.dashboard_text)
        splitter.addWidget(diag_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.tabs.addTab(page, "📊 Tổng quan")

    # ----------------------------------------------------
    # TAB 2: TTS STUDIO & PLAYGROUND
    # ----------------------------------------------------
    def _build_studio_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        config_box = QGroupBox("Cấu hình giọng đọc & Mô hình")
        cfg_layout = QGridLayout(config_box)
        cfg_layout.setSpacing(10)

        self.studio_model = QComboBox()
        self.studio_model.currentIndexChanged.connect(self._on_studio_model_changed)
        self.studio_lang = QComboBox()
        self.studio_voice = QComboBox()
        
        self.studio_speed = QSlider(Qt.Horizontal)
        self.studio_speed.setRange(5, 20)
        self.studio_speed.setValue(10)
        self.studio_speed.setEnabled(False)
        self.studio_speed.setToolTip("Runtime hiện chỉ hỗ trợ tốc độ 1.0x")
        self.studio_speed_label = QLabel("1.0x (cố định)")
        self.studio_speed.valueChanged.connect(lambda v: self.studio_speed_label.setText(f"{v/10:.1f}x"))

        cfg_layout.addWidget(QLabel("Mô hình TTS:"), 0, 0)
        cfg_layout.addWidget(self.studio_model, 0, 1)
        cfg_layout.addWidget(QLabel("Ngôn ngữ:"), 0, 2)
        cfg_layout.addWidget(self.studio_lang, 0, 3)
        cfg_layout.addWidget(QLabel("Giọng đọc (Voice):"), 0, 4)
        cfg_layout.addWidget(self.studio_voice, 0, 5)

        speed_box = QHBoxLayout()
        speed_box.addWidget(self.studio_speed)
        speed_box.addWidget(self.studio_speed_label)
        cfg_layout.addWidget(QLabel("Tốc độ đọc:"), 1, 0)
        cfg_layout.addLayout(speed_box, 1, 1, 1, 2)

        # Quick sample prompts buttons
        sample_bar = QHBoxLayout()
        sample_bar.addWidget(QLabel("Mẫu câu nhanh:"))
        btn_sample_vi = QPushButton("🇻🇳 Tiếng Việt mẫu")
        btn_sample_vi.clicked.connect(
            lambda: self._apply_studio_sample(
                "Sáng nay lúc 7 giờ 45, thời tiết rất đẹp, chúng ta cùng đi uống cà phê nhé!",
                "vi",
                "tts-vietnamese",
            )
        )
        btn_sample_en = QPushButton("🇺🇸 English sample")
        btn_sample_en.clicked.connect(
            lambda: self._apply_studio_sample(
                "Hello! Welcome to the high performance AI text to speech gateway.",
                "en",
                "tts-multilingual",
            )
        )
        sample_bar.addWidget(btn_sample_vi)
        sample_bar.addWidget(btn_sample_en)
        sample_bar.addStretch()
        cfg_layout.addLayout(sample_bar, 1, 3, 1, 3)

        layout.addWidget(config_box)

        # Text input area
        input_box = QGroupBox("Văn bản cần đọc")
        in_layout = QVBoxLayout(input_box)
        self.studio_text = QTextEdit()
        self.studio_text.setPlaceholderText("Nhập văn bản cần chuyển thành giọng nói tại đây...")
        self.studio_text.setPlainText("Sáng nay lúc 7 giờ 45, thời tiết rất đẹp, chúng ta cùng đi uống cà phê nhé!")
        in_layout.addWidget(self.studio_text)
        layout.addWidget(input_box, 1)

        # Playback & Action Controls
        act_box = QFrame()
        act_box.setStyleSheet("background-color: #171926; border: 1px solid #282c40; border-radius: 8px; padding: 8px;")
        act_layout = QHBoxLayout(act_box)

        self.btn_synth = QPushButton("▶ Sinh giọng & Nghe thử")
        self.btn_synth.setObjectName("primaryButton")
        self.btn_synth.clicked.connect(self.studio_synthesize)
        act_layout.addWidget(self.btn_synth)

        self.btn_translate_speak = QPushButton("🌐 Dịch qua LLM & Đọc")
        self.btn_translate_speak.clicked.connect(self.studio_translate_and_speak)
        act_layout.addWidget(self.btn_translate_speak)

        self.btn_stop_audio = QPushButton("⏹ Dừng phát")
        self.btn_stop_audio.clicked.connect(self.audio_player.stop)
        act_layout.addWidget(self.btn_stop_audio)

        self.btn_save_wav = QPushButton("💾 Tải file WAV")
        self.btn_save_wav.clicked.connect(self.studio_save_wav)
        act_layout.addWidget(self.btn_save_wav)

        self.studio_status = QLabel("Sẵn sàng")
        self.studio_status.setStyleSheet("color: #94a3b8; font-style: italic; margin-left: 10px;")
        act_layout.addWidget(self.studio_status)
        act_layout.addStretch()

        layout.addWidget(act_box)
        self.tabs.addTab(page, "🎙 TTS Studio")

    def _on_studio_model_changed(self) -> None:
        model = self.studio_model.currentData()
        if not isinstance(model, dict):
            return
        cap = model.get("capabilities") or {}
        
        self.studio_lang.blockSignals(True)
        self.studio_lang.clear()
        for lang in cap.get("supported_languages", ["vi", "en"]):
            self.studio_lang.addItem(str(lang))
        self.studio_lang.blockSignals(False)
        
        self.studio_voice.clear()
        voices = cap.get("preset_voice_names") or [cap.get("default_voice") or "default"]
        for voice in voices:
            self.studio_voice.addItem(str(voice))

    def _apply_studio_sample(self, text: str, language: str, model_id: str) -> None:
        self.studio_text.setPlainText(text)
        for index in range(self.studio_model.count()):
            model = self.studio_model.itemData(index)
            if isinstance(model, dict) and model.get("id") == model_id:
                self.studio_model.setCurrentIndex(index)
                break
        language_index = self.studio_lang.findText(language)
        if language_index >= 0:
            self.studio_lang.setCurrentIndex(language_index)

    def studio_synthesize(self) -> None:
        text = self.studio_text.toPlainText().strip()
        if not text:
            self.show_error("Vui lòng nhập văn bản cần đọc")
            return
        product_key = self.product_key.text().strip()
        if not product_key:
            self.studio_status.setText("❌ Cần Product API Key")
            self.show_error("Vui lòng nhập Product API Key có scope tts.generate")
            return
        model = self.studio_model.currentData()
        model_id = str(model.get("id") or "") if isinstance(model, dict) else ""
        if not model_id:
            self.studio_status.setText("❌ Chưa có mô hình TTS")
            self.show_error("Không có mô hình TTS nào trong model catalog hiện tại")
            return
        lang = self.studio_lang.currentText() or "vi"
        voice = self.studio_voice.currentText() or "default"
        speed = 1.0  # Currently server engines require speed=1.0

        self.studio_status.setText("⏳ Đang sinh giọng nói từ server...")
        self.btn_synth.setEnabled(False)

        body = {
            "model": model_id,
            "input": text,
            "language": lang,
            "voice": voice,
            "response_format": "wav",
            "speed": speed,
        }

        def on_done(res: dict[str, Any]) -> None:
            self.btn_synth.setEnabled(True)
            wav_data = res.get("data")
            if not wav_data or wav_data[:4] != b"RIFF":
                self.studio_status.setText("❌ Không nhận được định dạng WAV hợp lệ")
                return
            self.last_audio_bytes = wav_data
            self.audio_player.play_bytes(wav_data)
            headers = {
                str(name).lower(): value
                for name, value in (res.get("headers") or {}).items()
            }
            dur_ms = headers.get("x-tts-duration-ms", "—")
            gen_ms = headers.get("x-tts-generation-ms", "—")
            self.studio_status.setText(f"✅ Đã phát audio ({len(wav_data):,} bytes | Độ dài: {dur_ms}ms | Xử lý: {gen_ms}ms)")

        def on_error(err: str) -> None:
            self.btn_synth.setEnabled(True)
            self.studio_status.setText("❌ Lỗi sinh giọng")
            self.show_error(err)

        task = ApiTask(
            self.target.text(),
            "/v1/audio/speech",
            "POST",
            product_key,
            body,
            on_done,
            on_error,
            timeout=120.0,
            binary=True,
        )
        task.signals.done.connect(self._dispatch_api_result, Qt.QueuedConnection)
        self._active_tasks.add(task)
        self.pool.start(task)

    def studio_translate_and_speak(self) -> None:
        text = self.studio_text.toPlainText().strip()
        if not text:
            self.show_error("Vui lòng nhập văn bản cần dịch")
            return
        if not self.product_key.text().strip():
            self.studio_status.setText("❌ Cần Product API Key")
            self.show_error("Vui lòng nhập Product API Key có scope llm.translate và tts.generate")
            return
        
        # Determine target language: if currently Vietnamese, translate to English, else to Vietnamese
        current_lang = self.studio_lang.currentText() or "vi"
        target_lang = "en" if current_lang == "vi" else "vi"
        self.studio_status.setText(f"⏳ Đang dịch sang '{target_lang}' qua Ollama...")

        body = {
            "text": text,
            "source_language": "auto",
            "target_language": target_lang,
            "style": "neutral",
        }

        def on_translated(res: dict[str, Any]) -> None:
            translated_text = res.get("translation", "")
            if not translated_text:
                self.studio_status.setText("❌ Bản dịch trống")
                return
            
            self.studio_text.setPlainText(translated_text)
            
            # Switch to appropriate TTS model for the target language
            if target_lang != "vi":
                # Find multilingual / chatterbox model
                for i in range(self.studio_model.count()):
                    if "multilingual" in self.studio_model.itemText(i) or "chatterbox" in self.studio_model.itemText(i):
                        self.studio_model.setCurrentIndex(i)
                        break
            else:
                # Find vietnamese / vieneu model
                for i in range(self.studio_model.count()):
                    if "vietnamese" in self.studio_model.itemText(i) or "vieneu" in self.studio_model.itemText(i):
                        self.studio_model.setCurrentIndex(i)
                        break

            # Set target language in dropdown
            idx = self.studio_lang.findText(target_lang)
            if idx >= 0:
                self.studio_lang.setCurrentIndex(idx)

            # Trigger synthesis
            self.studio_synthesize()

        def on_translation_error(err: str) -> None:
            self.studio_status.setText("❌ Lỗi dịch LLM")
            self.show_error(err)

        self.call(
            "/v1/translations",
            "POST",
            "product",
            on_translated,
            body,
            error_callback=on_translation_error,
        )

    def studio_save_wav(self) -> None:
        if not self.last_audio_bytes:
            self.show_error("Chưa có file âm thanh nào vừa sinh để lưu")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Lưu file WAV", "speech_output.wav", "WAV Audio (*.wav)")
        if path:
            Path(path).write_bytes(self.last_audio_bytes)
            QMessageBox.information(self, "Thành công", f"Đã lưu file âm thanh tại:\n{path}")

    # ----------------------------------------------------
    # TAB 3: API KEYS
    # ----------------------------------------------------
    def _build_keys_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Key creation form
        create_box = QGroupBox("Tạo API Key mới")
        c_layout = QGridLayout(create_box)
        c_layout.setSpacing(8)

        self.key_label = QLineEdit()
        self.key_label.setPlaceholderText("Tên gợi nhớ (ví dụ: web-app-client)")
        self.key_note = QLineEdit()
        self.key_note.setPlaceholderText("Ghi chú chủ sở hữu")
        self.key_rate = QLineEdit("60")
        self.key_quota = QLineEdit("100000")
        self.key_credits = QLineEdit("1000000")

        c_layout.addWidget(QLabel("Label:"), 0, 0)
        c_layout.addWidget(self.key_label, 0, 1)
        c_layout.addWidget(QLabel("Ghi chú:"), 0, 2)
        c_layout.addWidget(self.key_note, 0, 3)

        c_layout.addWidget(QLabel("Rate limit/phút:"), 1, 0)
        c_layout.addWidget(self.key_rate, 1, 1)
        c_layout.addWidget(QLabel("Daily quota credits:"), 1, 2)
        c_layout.addWidget(self.key_quota, 1, 3)
        c_layout.addWidget(QLabel("Credits ban đầu:"), 1, 4)
        c_layout.addWidget(self.key_credits, 1, 5)

        self.scope_panel = QWidget()
        self.scope_layout = QGridLayout(self.scope_panel)
        c_layout.addWidget(QLabel("Scopes cấp quyền:"), 2, 0)
        c_layout.addWidget(self.scope_panel, 2, 1, 1, 4)

        btn_create_key = QPushButton("✨ Tạo API Key")
        btn_create_key.setObjectName("primaryButton")
        btn_create_key.clicked.connect(self.create_key)
        c_layout.addWidget(btn_create_key, 2, 5)

        layout.addWidget(create_box)
        self._render_scopes(SCOPE_HINTS)

        # Search & Filter bar for Keys
        filter_bar = QHBoxLayout()
        self.key_search = QLineEdit()
        self.key_search.setPlaceholderText("🔍 Tìm kiếm theo Label hoặc Prefix...")
        self.key_search.textChanged.connect(self._filter_keys_table)
        filter_bar.addWidget(self.key_search, 1)

        self.key_status_filter = QComboBox()
        self.key_status_filter.addItems(["Tất cả trạng thái", "Đang hoạt động (Enabled)", "Đã tắt (Disabled)", "Đã thu hồi (Revoked)"])
        self.key_status_filter.currentIndexChanged.connect(self._filter_keys_table)
        filter_bar.addWidget(self.key_status_filter)

        btn_refresh_keys = QPushButton("🔄 Tải lại")
        btn_refresh_keys.clicked.connect(self.load_keys)
        filter_bar.addWidget(btn_refresh_keys)
        layout.addLayout(filter_bar)

        # Keys Table
        self.key_table = QTableWidget(0, 6)
        self.key_table.setHorizontalHeaderLabels(["Prefix", "Label", "Scopes", "Credits / Daily Quota", "Trạng thái", "Ngày tạo"])
        self.key_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.key_table.horizontalHeader().setStretchLastSection(True)
        self.key_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.key_table, 1)

        # Action Buttons Row
        act_bar = QHBoxLayout()
        btn_toggle_key = QPushButton("⚡ Bật / Tắt Key")
        btn_toggle_key.clicked.connect(self.toggle_key)
        act_bar.addWidget(btn_toggle_key)

        btn_revoke_key = QPushButton("🚫 Thu hồi (Revoke)")
        btn_revoke_key.setObjectName("dangerButton")
        btn_revoke_key.clicked.connect(self.revoke_key)
        act_bar.addWidget(btn_revoke_key)

        btn_delete_key = QPushButton("🗑 Xóa vĩnh viễn")
        btn_delete_key.setObjectName("dangerButton")
        btn_delete_key.clicked.connect(self.delete_key)
        act_bar.addWidget(btn_delete_key)

        act_bar.addStretch()
        layout.addLayout(act_bar)

        self.tabs.addTab(page, "🔑 Quản lý API Keys")

    def _render_scopes(self, scopes: Any) -> None:
        while self.scope_layout.count():
            item = self.scope_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.scope_checks = {}
        for index, scope in enumerate(scopes):
            name = str(scope)
            check = QCheckBox(name)
            check.setChecked(name in {"llm.translate", "tts.generate", "usage.read"})
            self.scope_layout.addWidget(check, index // 3, index % 3)
            self.scope_checks[name] = check

    def create_key(self) -> None:
        scopes = [s for s, c in self.scope_checks.items() if c.isChecked()]
        if not scopes:
            self.show_error("Vui lòng chọn ít nhất một scope quyền hạn")
            return
        body: dict[str, Any] = {
            "scopes": scopes,
            "label": self.key_label.text().strip(),
            "owner_note": self.key_note.text().strip(),
        }
        for widget, name in (
            (self.key_rate, "rate_limit_per_minute"),
            (self.key_quota, "daily_quota_credits"),
            (self.key_credits, "initial_credits"),
        ):
            val = widget.text().strip()
            if val:
                try:
                    body[name] = int(val)
                except ValueError:
                    self.show_error(f"{name} phải là số nguyên")
                    return

        def done(data: dict[str, Any]) -> None:
            full_key = data.get("key", "")
            self._show_key_created_dialog(full_key, data)
            self.load_keys()

        self.call("/v1/admin/api-keys", "POST", "admin", done, body)

    def _show_key_created_dialog(self, full_key: str, data: dict[str, Any]) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("Tạo API Key thành công")
        msg.setIcon(QMessageBox.Information)
        msg.setText("<b>LƯU Ý QUAN TRỌNG:</b> Hãy copy Full Key này ngay; key bí mật chỉ hiển thị 1 lần duy nhất!")
        msg.setInformativeText(f"<b>Key:</b> <code>{full_key}</code>\n\nPrefix: {data.get('key_prefix')}\nLabel: {data.get('label')}")
        btn_copy = msg.addButton("📋 Copy Full Key", QMessageBox.ActionRole)
        msg.addButton("Đóng", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() == btn_copy:
            QApplication.clipboard().setText(full_key)
            self.statusBar().showMessage("Đã copy Full Key vào bộ nhớ tạm (Clipboard)", 5000)

    def load_keys(self) -> None:
        def done(data: dict[str, Any]) -> None:
            self.key_table.setRowCount(0)
            for key in data.get("keys", []):
                row = self.key_table.rowCount()
                self.key_table.insertRow(row)
                
                status = "Revoked" if key.get("revoked") else "Enabled" if key.get("enabled") else "Disabled"
                scopes_str = ", ".join(key.get("scopes", []))
                credits_str = f"{key.get('credits', 0):,} / {key.get('daily_quota_credits', 0):,}"
                created_str = (key.get("created_at") or "")[:19].replace("T", " ")

                items = (
                    key.get("key_prefix", ""),
                    key.get("label", ""),
                    scopes_str,
                    credits_str,
                    status,
                    created_str,
                )
                for col, val in enumerate(items):
                    item = QTableWidgetItem(str(val))
                    item.setData(Qt.UserRole, key.get("id"))
                    if col == 4:
                        if status == "Enabled":
                            item.setForeground(QColor("#10b981"))
                        elif status == "Disabled":
                            item.setForeground(QColor("#f59e0b"))
                        else:
                            item.setForeground(QColor("#ef4444"))
                    self.key_table.setItem(row, col, item)
            self._filter_keys_table()

        self.call("/v1/admin/api-keys?include_inactive=true", "GET", "admin", done)

    def _filter_keys_table(self) -> None:
        search_text = self.key_search.text().strip().lower()
        filter_status = self.key_status_filter.currentText()

        for row in range(self.key_table.rowCount()):
            prefix_item = self.key_table.item(row, 0)
            label_item = self.key_table.item(row, 1)
            status_item = self.key_table.item(row, 4)

            prefix = prefix_item.text().lower() if prefix_item else ""
            label = label_item.text().lower() if label_item else ""
            status = status_item.text() if status_item else ""

            match_search = not search_text or search_text in prefix or search_text in label
            match_status = (
                filter_status == "Tất cả trạng thái"
                or ("Enabled" in filter_status and status == "Enabled")
                or ("Disabled" in filter_status and status == "Disabled")
                or ("Revoked" in filter_status and status == "Revoked")
            )
            self.key_table.setRowHidden(row, not (match_search and match_status))

    def selected_key(self) -> str | None:
        rows = self.key_table.selectedItems()
        return rows[0].data(Qt.UserRole) if rows else None

    def toggle_key(self) -> None:
        key_id = self.selected_key()
        if not key_id:
            self.show_error("Vui lòng chọn một API key trong bảng")
            return
        row = self.key_table.currentRow()
        status = self.key_table.item(row, 4).text() if self.key_table.item(row, 4) else ""
        action = "enable" if status != "Enabled" else "disable"
        self.call(f"/v1/admin/api-keys/{key_id}/{action}", "POST", "admin", lambda _d: self.load_keys())

    def revoke_key(self) -> None:
        key_id = self.selected_key()
        if key_id and QMessageBox.question(self, "Xác nhận", "Bạn có chắc chắn muốn thu hồi (Revoke) key này?") == QMessageBox.Yes:
            self.call(f"/v1/admin/api-keys/{key_id}", "DELETE", "admin", lambda _d: self.load_keys())

    def delete_key(self) -> None:
        key_id = self.selected_key()
        if key_id and QMessageBox.question(self, "Xác nhận xóa", "XÓA VĨNH VIỄN key và toàn bộ lịch sử liên quan?") == QMessageBox.Yes:
            self.call(f"/v1/admin/api-keys/{key_id}/permanent", "DELETE", "admin", lambda _d: self.load_keys())

    # ----------------------------------------------------
    # TAB 4: MODELS & CAPABILITIES
    # ----------------------------------------------------
    def _build_models_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.model_table = QTableWidget(0, 5)
        self.model_table.setHorizontalHeaderLabels(["ID Mô hình", "Provider", "Khả dụng", "Ngôn ngữ hỗ trợ", "Danh sách giọng đọc"])
        self.model_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.model_table.horizontalHeader().setStretchLastSection(True)
        self.model_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.model_table.itemSelectionChanged.connect(self.model_selected)
        layout.addWidget(self.model_table, 2)

        detail_box = QGroupBox("Chi tiết mô hình & Tham số gọi API")
        d_layout = QVBoxLayout(detail_box)
        self.model_detail = QTextEdit()
        self.model_detail.setReadOnly(True)
        d_layout.addWidget(self.model_detail)
        layout.addWidget(detail_box, 1)

        self.tabs.addTab(page, "📦 Mô hình AI")

    def render_models(self, data: dict[str, Any]) -> None:
        models = data.get("models") or data.get("data") or []
        self.cached_models = models
        self.studio_models = [
            model
            for model in models
            if str(model.get("id") or "").startswith("tts-")
        ]
        self.model_table.setRowCount(0)
        
        self.studio_model.clear()

        available_count = 0
        for model in models:
            is_avail = bool(model.get("available"))
            if is_avail:
                available_count += 1
            cap = model.get("capabilities") or {}
            langs = cap.get("supported_languages") or []
            voices = cap.get("preset_voice_names") or [cap.get("default_voice") or "default"]
            
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)

            values = (
                model.get("id", ""),
                model.get("provider", ""),
                "Sẵn sàng" if is_avail else "Chưa sẵn sàng",
                ", ".join(map(str, langs)),
                ", ".join(map(str, voices)),
            )
            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                if col == 2:
                    item.setForeground(QColor("#10b981") if is_avail else QColor("#ef4444"))
                self.model_table.setItem(row, col, item)

        for model in self.studio_models:
            self.studio_model.addItem(
                f"{model.get('id')} ({model.get('provider')})",
                model,
            )

        for index in range(self.studio_model.count()):
            model = self.studio_model.itemData(index)
            if isinstance(model, dict) and model.get("id") == "tts-vietnamese":
                self.studio_model.setCurrentIndex(index)
                break

        self.card_models.set_value(f"{available_count} / {len(models)}", "#10b981" if available_count else "#ef4444")
        self._on_studio_model_changed()

    def model_selected(self) -> None:
        items = self.model_table.selectedItems()
        if not items:
            return
        row = self.model_table.currentRow()
        if row < 0 or row >= len(self.cached_models):
            return
        m = self.cached_models[row]
        self.model_detail.setPlainText(json.dumps(m, ensure_ascii=False, indent=2))

    # ----------------------------------------------------
    # TAB 5: JOBS & EVENTS
    # ----------------------------------------------------
    def _build_jobs_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        btn_refresh_jobs = QPushButton("🔄 Tải lại Jobs & Events")
        btn_refresh_jobs.clicked.connect(self.load_jobs_and_events)
        top_row.addWidget(btn_refresh_jobs)

        self.chk_auto_refresh = QCheckBox("Tự động làm mới (mỗi 5s)")
        self.chk_auto_refresh.stateChanged.connect(self._toggle_auto_refresh)
        top_row.addWidget(self.chk_auto_refresh)
        top_row.addStretch()
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Vertical)

        # Jobs table
        jobs_box = QGroupBox("Danh sách tác vụ gần đây (Jobs)")
        j_layout = QVBoxLayout(jobs_box)
        self.jobs_table = QTableWidget(0, 6)
        self.jobs_table.setHorizontalHeaderLabels(["Job ID", "Loại", "Provider", "Model", "Trạng thái", "Thời gian"])
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        j_layout.addWidget(self.jobs_table)
        splitter.addWidget(jobs_box)

        # Events list
        events_box = QGroupBox("Nhật ký sự kiện thời gian thực (Events)")
        e_layout = QVBoxLayout(events_box)
        self.activity_text = QTextEdit()
        self.activity_text.setReadOnly(True)
        e_layout.addWidget(self.activity_text)
        splitter.addWidget(events_box)

        layout.addWidget(splitter, 1)
        self.tabs.addTab(page, "📋 Giám sát Jobs & Events")

    def _toggle_auto_refresh(self, state: int) -> None:
        if state == Qt.Checked:
            self.auto_refresh_timer.start(5000)
        else:
            self.auto_refresh_timer.stop()

    def on_auto_refresh(self) -> None:
        if self.tabs.currentIndex() == 4:  # Jobs tab
            self.load_jobs_and_events()

    def load_jobs_and_events(self) -> None:
        def on_jobs(data: dict[str, Any]) -> None:
            jobs = data.get("jobs", [])
            self.jobs_table.setRowCount(0)
            for j in jobs:
                row = self.jobs_table.rowCount()
                self.jobs_table.insertRow(row)
                state = j.get("state", "")
                items = (
                    j.get("id", "")[:12],
                    j.get("kind", ""),
                    j.get("provider", ""),
                    j.get("model", ""),
                    state,
                    (j.get("created_at") or "")[:19].replace("T", " "),
                )
                for col, val in enumerate(items):
                    item = QTableWidgetItem(str(val))
                    if col == 4:
                        if state == "succeeded":
                            item.setForeground(QColor("#10b981"))
                        elif state == "failed":
                            item.setForeground(QColor("#ef4444"))
                        else:
                            item.setForeground(QColor("#60a5fa"))
                    self.jobs_table.setItem(row, col, item)

        def on_events(data: dict[str, Any]) -> None:
            self.activity_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

        self.call("/v1/admin/jobs", "GET", "admin", on_jobs)
        self.call("/v1/admin/events", "GET", "admin", on_events)

    # ----------------------------------------------------
    # TAB 6: USAGE & METRICS
    # ----------------------------------------------------
    def _build_usage_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        top_row = QHBoxLayout()
        btn_refresh_metrics = QPushButton("🔄 Tải lại thống kê")
        btn_refresh_metrics.clicked.connect(self.load_metrics)
        top_row.addWidget(btn_refresh_metrics)
        top_row.addStretch()
        layout.addLayout(top_row)

        # 1. Per-key breakdown table
        key_usage_box = QGroupBox("📊 Thống kê tiêu thụ chi tiết theo TỪNG API KEY")
        ku_layout = QVBoxLayout(key_usage_box)
        self.usage_key_table = QTableWidget(0, 7)
        self.usage_key_table.setHorizontalHeaderLabels([
            "Prefix Key", "Label", "Số Requests", "LLM Tokens", "Ký tự TTS", "Credits đã trừ", "Credits còn lại"
        ])
        self.usage_key_table.horizontalHeader().setStretchLastSection(True)
        self.usage_key_table.setSelectionBehavior(QTableWidget.SelectRows)
        ku_layout.addWidget(self.usage_key_table)
        layout.addWidget(key_usage_box, 2)

        # 2. System summary + raw JSON
        splitter = QSplitter(Qt.Horizontal)

        # Summary box
        summary_box = QGroupBox("Tổng kết số liệu toàn hệ thống")
        s_layout = QVBoxLayout(summary_box)
        self.metrics_summary = QTextEdit()
        self.metrics_summary.setReadOnly(True)
        s_layout.addWidget(self.metrics_summary)
        splitter.addWidget(summary_box)

        # Raw metrics JSON
        raw_box = QGroupBox("Dữ liệu Metrics chi tiết (JSON)")
        r_layout = QVBoxLayout(raw_box)
        self.usage_text = QTextEdit()
        self.usage_text.setReadOnly(True)
        r_layout.addWidget(self.usage_text)
        splitter.addWidget(raw_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.tabs.addTab(page, "📈 Thống kê & Sử dụng")

    def load_metrics(self) -> None:
        def on_metrics(data: dict[str, Any]) -> None:
            self.usage_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            totals = data.get("totals", {})
            lat = data.get("latency_ms", {})
            text = (
                f"Tổng số Request: {data.get('request_count', 0):,}\n"
                f"Tỷ lệ thành công: {data.get('success_rate', 0.0) * 100:.1f}%\n"
                f"Tỷ lệ lỗi: {data.get('error_rate', 0.0) * 100:.1f}%\n\n"
                f"--- SỐ LƯỢNG TIÊU THỤ TOÀN SERVER ---\n"
                f"Input Tokens (LLM): {totals.get('input_tokens', 0):,}\n"
                f"Output Tokens (LLM): {totals.get('output_tokens', 0):,}\n"
                f"Ký tự âm thanh (TTS): {totals.get('characters', 0):,}\n"
                f"Thời lượng audio: {totals.get('audio_duration_ms', 0) / 1000.0:.2f} giây\n"
                f"Tổng Credits đã trừ: {totals.get('credits', 0):,}\n\n"
                f"--- ĐỘ TRỄ (LATENCY) ---\n"
                f"p50 Total: {lat.get('p50_total', 0)} ms\n"
                f"p95 Total: {lat.get('p95_total', 0)} ms\n"
                f"p50 Chờ hàng đợi: {lat.get('p50_queue_wait', 0)} ms\n"
                f"p50 Xử lý sinh: {lat.get('p50_generation', 0)} ms\n"
            )
            self.metrics_summary.setPlainText(text)

        def on_usage_keys(data: dict[str, Any]) -> None:
            self.usage_key_table.setRowCount(0)
            for k in data.get("keys", []):
                row = self.usage_key_table.rowCount()
                self.usage_key_table.insertRow(row)
                tokens_str = f"{k.get('input_tokens', 0) + k.get('output_tokens', 0):,}"
                items = (
                    str(k.get("key_prefix", ""))[:14],
                    str(k.get("label", "")),
                    f"{k.get('events', 0):,}",
                    tokens_str,
                    f"{k.get('characters', 0):,}",
                    f"{k.get('credits', 0):,}",
                    f"{k.get('credits_remaining', 0):,}",
                )
                for col, val in enumerate(items):
                    item = QTableWidgetItem(str(val))
                    if col == 5:
                        item.setForeground(QColor("#f59e0b"))
                    elif col == 6:
                        item.setForeground(QColor("#10b981"))
                    self.usage_key_table.setItem(row, col, item)

        self.call("/v1/admin/metrics", "GET", "admin", on_metrics)
        self.call("/v1/admin/usage", "GET", "admin", on_usage_keys)

    # ----------------------------------------------------
    # TAB 7: OPERATIONS & LOGS
    # ----------------------------------------------------
    def _build_ops_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Server Subprocess Config
        proc_box = QGroupBox("Quản lý tiến trình Server (Local Subprocess)")
        p_layout = QGridLayout(proc_box)
        p_layout.setSpacing(8)

        self.bind_host = QLineEdit("127.0.0.1")
        self.bind_port = QSpinBox()
        self.bind_port.setRange(1, 65535)
        self.bind_port.setValue(8000)
        self.insecure = QCheckBox("Cho phép kết nối LAN không TLS (-AllowInsecureLan)")

        p_layout.addWidget(QLabel("Bind Host:"), 0, 0)
        p_layout.addWidget(self.bind_host, 0, 1)
        p_layout.addWidget(QLabel("Port:"), 0, 2)
        p_layout.addWidget(self.bind_port, 0, 3)
        p_layout.addWidget(self.insecure, 0, 4)

        btn_start = QPushButton("▶ Start Server")
        btn_start.setObjectName("successButton")
        btn_start.clicked.connect(self.start_server)

        btn_stop = QPushButton("⏹ Stop Server")
        btn_stop.setObjectName("dangerButton")
        btn_stop.clicked.connect(self.stop_server)

        btn_restart = QPushButton("🔄 Restart")
        btn_restart.clicked.connect(self.restart_server)

        p_layout.addWidget(btn_start, 1, 1)
        p_layout.addWidget(btn_stop, 1, 2)
        p_layout.addWidget(btn_restart, 1, 3)

        layout.addWidget(proc_box)

        # Maintenance Actions
        maint_box = QGroupBox("Tác vụ bảo trì & Quản trị hệ thống")
        m_layout = QHBoxLayout(maint_box)
        
        btn_backup = QPushButton("💾 Sao lưu Database (Backup)")
        btn_backup.clicked.connect(lambda: self.call("/v1/admin/backup", "POST", "admin", self.show_ops))
        m_layout.addWidget(btn_backup)

        btn_reset = QPushButton("🔄 Reset Runtime State")
        btn_reset.clicked.connect(lambda: self.call("/v1/admin/runtime/reset", "POST", "admin", self.show_ops))
        m_layout.addWidget(btn_reset)

        btn_build_info = QPushButton("ℹ Thông tin Build")
        btn_build_info.clicked.connect(lambda: self.call("/v1/admin/build-info", "GET", "admin", self.show_ops))
        m_layout.addWidget(btn_build_info)

        m_layout.addStretch()
        layout.addWidget(maint_box)

        # Console Log Viewer
        log_box = QGroupBox("Console & Server Output Log")
        l_layout = QVBoxLayout(log_box)

        log_ctrl = QHBoxLayout()
        btn_clear_log = QPushButton("🧹 Xóa màn hình log")
        btn_clear_log.clicked.connect(lambda: self.log.clear())
        self.chk_autoscroll = QCheckBox("Tự động cuộn xuống (Auto-scroll)")
        self.chk_autoscroll.setChecked(True)
        log_ctrl.addWidget(btn_clear_log)
        log_ctrl.addWidget(self.chk_autoscroll)
        log_ctrl.addStretch()
        l_layout.addLayout(log_ctrl)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background-color: #0b0c13; color: #a6accd; font-family: 'Consolas', monospace; font-size: 12px;")
        l_layout.addWidget(self.log)

        layout.addWidget(log_box, 1)
        self.tabs.addTab(page, "⚙️ Vận hành & Logs")

    # ----------------------------------------------------
    # API & DISPATCH LOGIC
    # ----------------------------------------------------
    def call(
        self,
        path: str,
        method: str,
        key_kind: str,
        callback: Callable[[dict[str, Any]], None],
        body: Any = None,
        error_callback: Callable[[str], None] | None = None,
    ) -> None:
        if key_kind == "admin":
            key = self.admin_key.text().strip()
        elif key_kind == "product":
            key = self.product_key.text().strip()
        else:
            key = ""
        task = ApiTask(
            self.target.text(),
            path,
            method,
            key,
            body,
            callback,
            error_callback or self.show_error,
        )
        task.signals.done.connect(self._dispatch_api_result, Qt.QueuedConnection)
        self._active_tasks.add(task)
        self.pool.start(task)

    def _dispatch_api_result(self, result: Any) -> None:
        status, payload, error, callback, error_callback, task = result
        self._active_tasks.discard(task)
        if error:
            error_callback(error)
        elif status >= 400:
            if isinstance(payload, (dict, list)):
                msg = json.dumps(payload, ensure_ascii=False, default=str)
            else:
                msg = str(payload)
            error_callback(f"HTTP {status}: {msg}")
        else:
            callback(payload)

    def show_error(self, error: str) -> None:
        self.statusBar().showMessage(f"Lỗi: {error}", 10000)
        if self._error_dialog is not None:
            self._error_dialog.setText(error)
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("TTS Server Admin")
        dialog.setIcon(QMessageBox.Critical)
        dialog.setText(error)
        dialog.setStandardButtons(QMessageBox.Ok)
        dialog.finished.connect(self._clear_error_dialog)
        self._error_dialog = dialog
        dialog.open()

    def _clear_error_dialog(self, _result: int) -> None:
        self._error_dialog = None

    def show_ops(self, data: dict[str, Any]) -> None:
        QMessageBox.information(self, "Kết quả vận hành", json.dumps(data, ensure_ascii=False, indent=2))

    def connect(self) -> None:
        if not self.admin_key.text().strip():
            self.show_error("Vui lòng nhập Admin API Key có scope admin.full")
            return
        self.statusBar().showMessage("Đang kết nối tới server...", 3000)

        def authorized(data: dict[str, Any], scopes_data: dict[str, Any]) -> None:
            self.status_dot.setText("● Đã kết nối")
            self.status_dot.setStyleSheet("color: #10b981; font-weight: bold; font-size: 12px;")
            
            self.card_status.set_value("ONLINE", "#10b981")
            self.card_status.set_subtitle(f"{data.get('service', 'tts-server')} v{data.get('version', '0.1.0')}")
            
            self.dashboard_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

            # Chain refresh sub-components
            self.call("/v1/admin/models", "GET", "admin", self.render_models)
            self.call("/v1/admin/overview", "GET", "admin", self._update_overview)
            self._render_scopes(scopes_data.get("scopes", []))
            self.load_keys()
            self.load_metrics()

            if self.chk_realtime_dashboard.isChecked():
                self.realtime_timer.start(2000)

        def server_live(data: dict[str, Any]) -> None:
            self.call(
                "/v1/admin/scopes",
                "GET",
                "admin",
                lambda scopes_data: authorized(data, scopes_data),
            )

        self.call("/health/live", "GET", "public", server_live)

    def _toggle_realtime_monitor(self, state: int) -> None:
        if state == Qt.Checked and self.card_status.val_label.text() == "ONLINE":
            self.realtime_timer.start(2000)
        else:
            self.realtime_timer.stop()

    def _poll_realtime(self) -> None:
        if self.card_status.val_label.text() != "ONLINE" or self._realtime_inflight:
            return
        self._realtime_inflight = True

        def done(data: dict[str, Any]) -> None:
            self._realtime_inflight = False
            self._update_overview(data)

        def failed(error: str) -> None:
            self._realtime_inflight = False
            self.statusBar().showMessage(f"Realtime tạm dừng: {error}", 10000)

        self.call(
            "/v1/admin/overview",
            "GET",
            "admin",
            done,
            error_callback=failed,
        )

    def _update_overview(self, data: dict[str, Any]) -> None:
        # Update RAM info
        ram = data.get("ram") or {}
        ram_total = ram.get("total_bytes") or 0
        ram_used = ram.get("used_bytes") or 0
        ram_pct = ram.get("utilization_percent")
        if ram_pct is None and ram_total:
            ram_pct = int((ram_used / ram_total) * 100)
        ram_pct = ram_pct or 0
        self.ram_bar.setValue(ram_pct)
        if ram_total:
            self.ram_detail.setText(f"RAM: {ram_used / (1024**3):.1f} GB / {ram_total / (1024**3):.1f} GB ({ram_pct}%)")
        else:
            self.ram_detail.setText("RAM: Đang cập nhật...")

        # Update GPU info
        gpu = data.get("gpu") or {}
        if gpu.get("available"):
            gpu_name = gpu.get("name") or "CUDA GPU"
            vram_total = int(gpu.get("total_memory_mb") or 0) * 1024 * 1024
            vram_used = int(gpu.get("used_memory_mb") or 0) * 1024 * 1024
            vram_pct = int((vram_used / vram_total * 100)) if vram_total else 0
            self.card_gpu.set_value(f"{gpu_name}", "#818cf8")
            self.card_gpu.set_subtitle(f"VRAM: {vram_used // (1024*1024):,} / {vram_total // (1024*1024):,} MB ({vram_pct}%)")
            self.vram_bar.setValue(vram_pct)
            self.vram_detail.setText(f"VRAM: {vram_used // (1024*1024):,} MB / {vram_total // (1024*1024):,} MB")
        else:
            self.card_gpu.set_value("CPU Only", "#94a3b8")
            self.card_gpu.set_subtitle("Không cấu hình GPU")
            self.vram_bar.setValue(0)
            self.vram_detail.setText("VRAM: Không có GPU")

        # Scheduler
        sched = data.get("scheduler") or {}
        active_jobs = sched.get("running", 0)
        gpu_q = sched.get("gpu_queue_depth", 0)
        cpu_q = sched.get("cpu_queue_depth", 0)
        self.card_scheduler.set_value(f"Active: {active_jobs}", "#10b981" if active_jobs else "#ffffff")
        self.card_scheduler.set_subtitle(f"Queue GPU: {gpu_q} | Queue CPU: {cpu_q}")

    def toggle_server_process(self) -> None:
        if self.server.running():
            self.stop_server()
        else:
            self.start_server()

    def start_server(self) -> None:
        try:
            self.server.start(self.bind_host.text().strip(), self.bind_port.value(), self.insecure.isChecked())
            self.append_log(f"[UI] Đang khởi động server trên {self.bind_host.text().strip()}:{self.bind_port.value()}...")
            self.statusBar().showMessage("Đang chờ server khởi động hoàn tất...", 0)
            self.btn_connect.setEnabled(False)
            self.btn_toggle_server.setText("⏹ Dừng Server")
            self.btn_toggle_server.setObjectName("dangerButton")
            self.btn_toggle_server.setStyle(self.btn_toggle_server.style())
            self._startup_probe_attempts = 0
            QTimer.singleShot(500, self._probe_server_startup)
        except Exception as exc:
            self.show_error(str(exc))

    def _probe_server_startup(self) -> None:
        if not self.server.running():
            self.btn_connect.setEnabled(True)
            self.statusBar().showMessage("Tiến trình server đã dừng; xem tab Vận hành & Logs.", 10000)
            self.btn_toggle_server.setText("▶ Bật Server")
            self.btn_toggle_server.setObjectName("successButton")
            self.btn_toggle_server.setStyle(self.btn_toggle_server.style())
            return

        self._startup_probe_attempts += 1

        def ready(_data: dict[str, Any]) -> None:
            self.btn_connect.setEnabled(True)
            self.statusBar().showMessage("Server đã sẵn sàng.", 5000)
            if self.admin_key.text().strip():
                self.connect()
            else:
                self.status_dot.setText("● Server sẵn sàng — cần Admin Key")
                self.status_dot.setStyleSheet("color: #f59e0b; font-weight: bold; font-size: 12px;")

        def retry(_error: str) -> None:
            if self._startup_probe_attempts >= 60:
                self.btn_connect.setEnabled(True)
                self.statusBar().showMessage("Server chưa sẵn sàng sau 60 giây; xem tab Vận hành & Logs.", 10000)
                return
            QTimer.singleShot(1000, self._probe_server_startup)

        self.call("/health/live", "GET", "public", ready, error_callback=retry)

    def stop_server(self) -> None:
        self.server.stop()
        self.append_log("[UI] Server đã dừng.")
        self.btn_connect.setEnabled(True)
        self.btn_toggle_server.setText("▶ Bật Server")
        self.btn_toggle_server.setObjectName("successButton")
        self.btn_toggle_server.setStyle(self.btn_toggle_server.style())
        self.status_dot.setText("● Offline")
        self.status_dot.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 12px;")

    def restart_server(self) -> None:
        self.stop_server()
        QTimer.singleShot(1000, self.start_server)

    def append_log(self, line: str) -> None:
        self.log.append(line)
        if self.chk_autoscroll.isChecked():
            self.log.moveCursor(QTextCursor.End)

    def closeEvent(self, event: Any) -> None:
        if self.server.running() and QMessageBox.question(self, "Thoát", "Dừng tiến trình server do UI khởi động trước khi thoát?") == QMessageBox.Yes:
            self.server.stop()
        event.accept()


def main() -> None:
    parser = argparse.ArgumentParser(description="Native PySide6 desktop UI for tts-server")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps({
            "pyside6": True,
            "run_server_script": RUN_SERVER.is_file(),
            "project_root": str(PROJECT_ROOT),
        }))
        return
    print(f"[UI] {ensure_ollama_started()}", flush=True)
    app = QApplication([])
    app.setStyle("Fusion")
    window = AdminWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
