# TTS Server Admin UI (desktop)

Ứng dụng desktop Python/PySide6 độc lập để kết nối và quản lý server TTS. UI này **không import, không sửa và không ghi vào code hiện tại**; nó gọi HTTP endpoint hiện có và có thể khởi động script server hiện có. Web UI cũ đã được xóa.

## Chạy

Từ thư mục dự án:

```powershell
.\server_admin_ui\run_desktop.ps1
```

Launcher dùng `.venv` của dự án và kiểm tra PySide6 trước khi mở cửa sổ. Có thể kiểm tra runtime trước:

```powershell
.venv\Scripts\python.exe server_admin_ui\server.py --self-test
```

Nhập URL server, admin API key và product API key trong cửa sổ desktop. Key chỉ nằm trong bộ nhớ process, không được lưu vào file.

## Chức năng

- Dashboard: live/health, service, GPU, số model khả dụng.
- Models & capabilities: đọc động `/v1/models`, languages, preset/default voice, options, runtime và unsupported features.
- API keys: đọc scopes, tạo key, enable/disable, revoke, xóa vĩnh viễn; full key chỉ hiển thị sau lúc tạo.
- Vận hành: overview, GPU, build-info, refresh models, database backup, runtime reset.
- Jobs/events: xem scheduler jobs và event buffer.
- Usage/metrics: admin usage, metrics và product usage.
- Server process: start/stop/restart `scripts/run_server.ps1`, chọn bind host/port và tùy chọn insecure LAN.

Mặc định UI không mở cổng mạng; đây là ứng dụng desktop. Khi bấm Start, UI chạy server bằng `.venv` và script hiện có. Dừng UI có thể dừng process server do UI khởi động. API key không được ghi vào log hoặc file. Dependency desktop: `PySide6` (đã cài trong `.venv`).
