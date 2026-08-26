"""Native PySide6 desktop UI for an existing tts-server.

The UI is independent: it calls the public HTTP API and can launch the
existing PowerShell server script. It never imports or edits the server code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SERVER = PROJECT_ROOT / "scripts" / "run_server.ps1"
SCOPE_HINTS = ("admin.full", "llm.generate", "llm.translate", "tts.generate", "tts.clone", "usage.read")


def request_json(target: str, path: str, method: str = "GET", api_key: str = "", body: Any = None) -> tuple[int, dict[str, Any]]:
    base = target.strip().rstrip("/")
    target_parts = urllib.parse.urlsplit(base)
    if target_parts.scheme not in {"http", "https"} or not target_parts.netloc:
        raise ValueError("Server URL phải là http(s) URL hợp lệ")
    parts = urllib.parse.urlsplit(path)
    url = base + (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    headers = {"Accept": "application/json"}
    if api_key.strip(): headers["Authorization"] = f"Bearer {api_key.strip()}"
    data = None
    if body is not None and method.upper() != "GET":
        data = json.dumps(body, ensure_ascii=False).encode("utf-8"); headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method.upper()), timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectionError(str(exc)) from exc
    try: value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError): value = {"raw": raw.decode("utf-8", errors="replace")[:4000]}
    return status, value if isinstance(value, dict) else {"value": value}


class TaskSignals(QObject):
    # Emit one opaque result object so the AdminWindow QObject receives the
    # callback on the Qt GUI thread instead of mutating widgets from a worker.
    done = Signal(object)


class ApiTask(QRunnable):
    def __init__(self, target: str, path: str, method: str, key: str, body: Any, callback: Callable[[Any], None], error: Callable[[str], None]) -> None:
        super().__init__(); self.target, self.path, self.method, self.key, self.body = target, path, method, key, body; self.callback, self.error = callback, error; self.signals = TaskSignals()
    def run(self) -> None:
        try:
            status, payload = request_json(self.target, self.path, self.method, self.key, self.body)
            self.signals.done.emit((status, payload, None, self.callback, self.error, self))
        except Exception as exc:
            self.signals.done.emit((0, {}, str(exc), self.callback, self.error, self))


class ProcessSignals(QObject):
    output = Signal(str)


class ServerProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None; self.signals = ProcessSignals(); self._lock = threading.Lock()
    def running(self) -> bool: return self.process is not None and self.process.poll() is None
    def start(self, host: str, port: int, insecure_lan: bool) -> None:
        with self._lock:
            if self.running(): raise RuntimeError("Server đã đang chạy")
            if not RUN_SERVER.is_file(): raise FileNotFoundError(str(RUN_SERVER))
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(RUN_SERVER), "-HostAddress", host, "-PortNumber", str(port)]
            if insecure_lan: command.append("-AllowInsecureLan")
            self.process = subprocess.Popen(command, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
    def _read(self, process: subprocess.Popen[str]) -> None:
        if process.stdout:
            for line in process.stdout: self.signals.output.emit(line.rstrip())
    def stop(self) -> None:
        with self._lock:
            process = self.process
            if process is None: return
            if process.poll() is None:
                if os.name == "nt": subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
                else: process.terminate()
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired: process.kill()
            self.process = None


class AdminWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self.setWindowTitle("TTS Server Admin"); self.resize(1220, 800); self.pool = QThreadPool.globalInstance(); self.server = ServerProcess(); self.server.signals.output.connect(self.append_log); self.scope_checks: dict[str, QCheckBox] = {}
        self._active_tasks: set[ApiTask] = set()
        self._build()
    def _build(self) -> None:
        central = QWidget(); root = QVBoxLayout(central); self.setCentralWidget(central)
        connection = QGroupBox("Kết nối server"); form=QGridLayout(connection); self.target=QLineEdit("http://127.0.0.1:8000"); self.admin_key=QLineEdit(); self.admin_key.setEchoMode(QLineEdit.Password); self.product_key=QLineEdit(); self.product_key.setEchoMode(QLineEdit.Password); start=QPushButton("Bật server"); start.clicked.connect(self.start_server); connect=QPushButton("Kết nối / Refresh"); connect.clicked.connect(self.connect)
        form.addWidget(QLabel("Server URL"),0,0); form.addWidget(self.target,0,1); form.addWidget(QLabel("Admin API key"),0,2); form.addWidget(self.admin_key,0,3); form.addWidget(QLabel("Product API key"),1,0); form.addWidget(self.product_key,1,1); form.addWidget(start,1,2); form.addWidget(connect,1,3); form.setColumnStretch(1,1); form.setColumnStretch(3,1); root.addWidget(connection)
        self.tabs=QTabWidget(); root.addWidget(self.tabs); self._dashboard(); self._models(); self._keys(); self._ops(); self._activity(); self._usage()
    def _dashboard(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); cards=QHBoxLayout(); self.cards={}
        for name in ("Live","Service","Models","GPU"):
            box=QGroupBox(name); v=QVBoxLayout(box); label=QLabel("—"); label.setStyleSheet("font-size:18px;font-weight:bold"); v.addWidget(label); cards.addWidget(box); self.cards[name]=label
        layout.addLayout(cards); row=QHBoxLayout(); b=QPushButton("Refresh"); b.clicked.connect(self.connect); row.addWidget(b); b=QPushButton("Health chi tiết"); b.clicked.connect(lambda: self.call("/health","GET","product",self.show_dashboard)); row.addWidget(b); layout.addLayout(row); self.dashboard_text=QTextEdit(); self.dashboard_text.setReadOnly(True); layout.addWidget(self.dashboard_text); self.tabs.addTab(page,"Tổng quan")
    def _models(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); self.model_table=QTableWidget(0,5); self.model_table.setHorizontalHeaderLabels(["Model","Provider","Available","Languages","Voices"]); self.model_table.horizontalHeader().setStretchLastSection(True); self.model_table.itemSelectionChanged.connect(self.model_selected); layout.addWidget(self.model_table); self.model_detail=QTextEdit(); self.model_detail.setReadOnly(True); layout.addWidget(self.model_detail); self.tabs.addTab(page,"Models & capabilities")
    def _keys(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); box=QGroupBox("Tạo API key"); form=QFormLayout(box); self.key_label=QLineEdit(); self.key_note=QLineEdit(); self.key_rate=QLineEdit(); self.key_quota=QLineEdit(); self.key_credits=QLineEdit(); form.addRow("Label",self.key_label); form.addRow("Owner note",self.key_note); form.addRow("Rate limit/min",self.key_rate); form.addRow("Daily quota",self.key_quota); form.addRow("Initial credits",self.key_credits); self.scope_panel=QWidget(); self.scope_panel.setMinimumHeight(78); self.scope_layout=QGridLayout(self.scope_panel); form.addRow("Scopes",self.scope_panel); scope_refresh=QPushButton("Tải scopes từ server"); scope_refresh.clicked.connect(self.load_scopes); form.addRow(scope_refresh); create=QPushButton("Tạo key"); create.clicked.connect(self.create_key); form.addRow(create); self.new_key=QTextEdit(); self.new_key.setReadOnly(True); self.new_key.setMaximumHeight(100); form.addRow("Full key",self.new_key); layout.addWidget(box); self._render_scopes(SCOPE_HINTS)
        self.key_table=QTableWidget(0,5); self.key_table.setHorizontalHeaderLabels(["Prefix","Label","Scopes","Credits","Status"]); self.key_table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.key_table); actions=QHBoxLayout(); for_text=(("Refresh",self.load_keys),("Enable/Disable",self.toggle_key),("Revoke",self.revoke_key),("Delete permanently",self.delete_key));
        for text, callback in for_text: button=QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        layout.addLayout(actions); self.tabs.addTab(page,"API keys")
    def _ops(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); box=QGroupBox("Server process"); row=QHBoxLayout(box); self.bind_host=QLineEdit("127.0.0.1"); self.bind_port=QSpinBox(); self.bind_port.setRange(1,65535); self.bind_port.setValue(8000); self.insecure=QCheckBox("Allow insecure LAN"); row.addWidget(QLabel("Host")); row.addWidget(self.bind_host); row.addWidget(QLabel("Port")); row.addWidget(self.bind_port); row.addWidget(self.insecure)
        for text, callback in (("Start",self.start_server),("Stop",self.stop_server),("Restart",self.restart_server)): button=QPushButton(text); button.clicked.connect(callback); row.addWidget(button)
        layout.addWidget(box); self.log=QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(150); layout.addWidget(self.log); api=QHBoxLayout()
        for text,path,method in (("Overview","/v1/admin/overview","GET"),("GPU","/v1/admin/gpu","GET"),("Build info","/v1/admin/build-info","GET"),("Backup","/v1/admin/backup","POST"),("Reset runtime","/v1/admin/runtime/reset","POST")): button=QPushButton(text); button.clicked.connect(lambda _checked=False,p=path,m=method:self.call(p,m,"admin",self.show_ops)); api.addWidget(button)
        layout.addLayout(api); self.ops_text=QTextEdit(); self.ops_text.setReadOnly(True); layout.addWidget(self.ops_text); self.tabs.addTab(page,"Vận hành")
    def _activity(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); row=QHBoxLayout()
        for text,path in (("Jobs","/v1/admin/jobs"),("Events","/v1/admin/events")): button=QPushButton(text); button.clicked.connect(lambda _checked=False,p=path:self.call(p,"GET","admin",self.show_activity)); row.addWidget(button)
        layout.addLayout(row); self.activity_text=QTextEdit(); self.activity_text.setReadOnly(True); layout.addWidget(self.activity_text); self.tabs.addTab(page,"Jobs / events")
    def _usage(self) -> None:
        page=QWidget(); layout=QVBoxLayout(page); row=QHBoxLayout()
        for text,path,key in (("Admin usage","/v1/admin/usage","admin"),("Metrics","/v1/admin/metrics","admin"),("Product usage","/v1/usage","product")): button=QPushButton(text); button.clicked.connect(lambda _checked=False,p=path,k=key:self.call(p,"GET",k,self.show_usage)); row.addWidget(button)
        layout.addLayout(row); self.usage_text=QTextEdit(); self.usage_text.setReadOnly(True); layout.addWidget(self.usage_text); self.tabs.addTab(page,"Usage / metrics")
    def call(self,path:str,method:str,key_kind:str,callback:Callable[[dict[str,Any]],None],body:Any=None)->None:
        key=self.admin_key.text() if key_kind=="admin" else (self.product_key.text() or self.admin_key.text())
        task=ApiTask(self.target.text(),path,method,key,body,callback,lambda error:self.show_error(error))
        task.signals.done.connect(self._dispatch_api_result, Qt.QueuedConnection)
        self._active_tasks.add(task)
        self.pool.start(task)
    def _dispatch_api_result(self, result:Any)->None:
        status, payload, error, callback, error_callback, task = result
        self._active_tasks.discard(task)
        if error:
            error_callback(error)
        elif status >= 400:
            error_callback(f"HTTP {status}: {json.dumps(payload, ensure_ascii=False)}")
        else:
            callback(payload)
    def show_error(self,error:str)->None: self.statusBar().showMessage(error,10000); QMessageBox.critical(self,"TTS Server Admin",error)
    def show_dashboard(self,data:dict[str,Any])->None: self.dashboard_text.setPlainText(json.dumps(data,ensure_ascii=False,indent=2))
    def show_ops(self,data:dict[str,Any])->None: self.ops_text.setPlainText(json.dumps(data,ensure_ascii=False,indent=2))
    def show_activity(self,data:dict[str,Any])->None: self.activity_text.setPlainText(json.dumps(data,ensure_ascii=False,indent=2))
    def show_usage(self,data:dict[str,Any])->None: self.usage_text.setPlainText(json.dumps(data,ensure_ascii=False,indent=2))
    def connect(self)->None:
        def done(data:dict[str,Any])->None:
            self.cards["Live"].setText("OK")
            self.cards["Service"].setText(str(data.get("service","—")))
            self.show_dashboard(data)
            self.call("/v1/models","GET","product",self.render_models)
            self.call("/v1/admin/gpu","GET","admin",lambda gpu:self.cards["GPU"].setText(str(gpu.get("name") or ("available" if gpu.get("available") else "unavailable"))))
            self.load_scopes()
            self.load_keys()
        self.call("/health/live","GET","product",done)
    def render_models(self,data:dict[str,Any])->None:
        models=data.get("models") or data.get("data") or []; self.model_table.setRowCount(0)
        for model in models:
            cap=model.get("capabilities") or {}; langs=cap.get("supported_languages") or []; voices=cap.get("preset_voice_names") or [cap.get("default_voice") or "—"]; row=self.model_table.rowCount(); self.model_table.insertRow(row); values=(model.get("id",""),model.get("provider",""),"yes" if model.get("available") else "no",", ".join(map(str,langs)),", ".join(map(str,voices)))
            for col,value in enumerate(values): self.model_table.setItem(row,col,QTableWidgetItem(str(value)))
        self.cards["Models"].setText(str(sum(1 for model in models if model.get("available"))))
    def model_selected(self)->None:
        items=self.model_table.selectedItems()
        if items: self.model_detail.setPlainText("Model: "+items[0].text()+"\nProvider: "+items[1].text()+"\nLanguages: "+items[3].text()+"\nVoices: "+items[4].text())
    def load_scopes(self)->None:
        def done(data:dict[str,Any])->None:
            self._render_scopes(data.get("scopes", []))
        self.call("/v1/admin/scopes","GET","admin",done)
    def _render_scopes(self, scopes:Any)->None:
        while self.scope_layout.count():
            item=self.scope_layout.takeAt(0)
            if item.widget() is not None: item.widget().deleteLater()
        self.scope_checks={}
        for index, scope in enumerate(scopes):
            name=str(scope); check=QCheckBox(name); check.setChecked(name in {"llm.translate","tts.generate"}); self.scope_layout.addWidget(check,index//3,index%3); self.scope_checks[name]=check
    def create_key(self)->None:
        scopes=[scope for scope,check in self.scope_checks.items() if check.isChecked()]
        if not scopes: self.show_error("Chọn ít nhất một scope"); return
        body={"scopes":scopes,"label":self.key_label.text(),"owner_note":self.key_note.text()}
        for widget,name in ((self.key_rate,"rate_limit_per_minute"),(self.key_quota,"daily_quota_credits"),(self.key_credits,"initial_credits")):
            if widget.text().strip():
                try: body[name]=int(widget.text())
                except ValueError: self.show_error(f"{name} phải là số"); return
        def done(data:dict[str,Any])->None: self.new_key.setPlainText(json.dumps({"warning":"Lưu full key ngay; chỉ hiển thị một lần","key":data.get("key"),"metadata":data},ensure_ascii=False,indent=2)); self.load_keys()
        self.call("/v1/admin/api-keys","POST","admin",done,body)
    def load_keys(self)->None:
        def done(data:dict[str,Any])->None:
            self.key_table.setRowCount(0)
            for key in data.get("keys",[]):
                row=self.key_table.rowCount(); self.key_table.insertRow(row); status="revoked" if key.get("revoked") else "enabled" if key.get("enabled") else "disabled"; values=(key.get("key_prefix",""),key.get("label",""),", ".join(key.get("scopes",[])),f"{key.get('credits','—')} / {key.get('daily_quota_credits','—')}",status)
                for col,value in enumerate(values): self.key_table.setItem(row,col,QTableWidgetItem(str(value))); self.key_table.item(row,col).setData(Qt.UserRole,key.get("id"))
        self.call("/v1/admin/api-keys?include_inactive=true","GET","admin",done)
    def selected_key(self)->str|None:
        rows=self.key_table.selectedItems(); return rows[0].data(Qt.UserRole) if rows else None
    def toggle_key(self)->None:
        key=self.selected_key()
        if not key: self.show_error("Chọn một key"); return
        row=self.key_table.currentRow(); status=self.key_table.item(row,4).text(); action="enable" if status!="enabled" else "disable"; self.call(f"/v1/admin/api-keys/{key}/{action}","POST","admin",lambda _data:self.load_keys())
    def revoke_key(self)->None:
        key=self.selected_key()
        if key and QMessageBox.question(self,"Xác nhận","Revoke key này?")==QMessageBox.Yes: self.call(f"/v1/admin/api-keys/{key}","DELETE","admin",lambda _data:self.load_keys())
    def delete_key(self)->None:
        key=self.selected_key()
        if key and QMessageBox.question(self,"Xác nhận","Xóa vĩnh viễn key này?")==QMessageBox.Yes: self.call(f"/v1/admin/api-keys/{key}/permanent","DELETE","admin",lambda _data:self.load_keys())
    def start_server(self)->None:
        try: self.server.start(self.bind_host.text().strip(),self.bind_port.value(),self.insecure.isChecked()); self.append_log("Starting server…")
        except Exception as exc: self.show_error(str(exc))
    def stop_server(self)->None: self.server.stop(); self.append_log("Server stopped")
    def restart_server(self)->None: self.stop_server(); self.start_server()
    def append_log(self,line:str)->None: self.log.append(line)
    def closeEvent(self,event:Any)->None:
        if self.server.running() and QMessageBox.question(self,"Thoát","Dừng server do UI khởi động?")==QMessageBox.Yes: self.server.stop()
        event.accept()


def main() -> None:
    parser=argparse.ArgumentParser(description="Native PySide6 desktop UI for tts-server"); parser.add_argument("--self-test",action="store_true"); args=parser.parse_args()
    if args.self_test: print(json.dumps({"pyside6":True,"run_server_script":RUN_SERVER.is_file(),"project_root":str(PROJECT_ROOT)})); return
    app=QApplication([]); app.setStyle("Fusion"); window=AdminWindow(); window.show(); raise SystemExit(app.exec())


if __name__=="__main__": main()
