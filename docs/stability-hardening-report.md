# Stability hardening report

Ngày kiểm tra: 2026-08-26  
Runtime: Windows, Python 3.13.14, RTX 3060 12 GB, CUDA 12.4

## Đã xử lý

- Manifest model được ghi atomic và được kiểm tra tồn tại, path containment, kích thước và SHA-256.
- Bootstrap trả exit code lỗi nếu model bị `failed`/`blocked`.
- Registry readiness mặc định không kích hoạt worker/model; deep probe là tùy chọn.
- Chuẩn hóa language (`vi-VN` → `vi`), từ chối input chỉ có whitespace, và hoàn tiền reservation khi request bị cancel/fail.
- Tính credit dùng tổng `token_credits + char_credits`; migration legacy giữ an toàn các reservation còn tham chiếu.
- CORS expose toàn bộ `X-TTS-*` headers.
- Mặc định Ollama `keep_alive=0`, GPU concurrency = 1.
- Runtime venv cô lập khỏi global Python packages; Windows dùng Torch CUDA 12.4 có điều kiện trong lock.
- Chatterbox xử lý namespace collision giữa package `perth` của VieNeu và watermark implementation của Resemble Perth.

## Kết quả xác minh

- `uv pip check`: pass (154 packages).
- `uv lock --check`: pass.
- `pytest`: **13 passed**.
- `compileall`: pass.
- Bootstrap/model smoke (sử dụng cache model hiện có): **3/3 available**.
  - Ollama: smoke pass, digest `64bfc813...`, 6,594,474,464 bytes.
  - Chatterbox: smoke pass, audio 1,360 ms, processing 38,242 ms.
  - VieNeu: smoke pass, audio 720 ms, processing 528 ms.
- HTTP E2E thật: `failed_checks=[]`; admin lifecycle, translation, chat, TTS vi/en, usage, backup, runtime reset và revoke key đều pass.
  - VieNeu WAV: 1,021,484 bytes.
  - Chatterbox WAV: 787,280 bytes.
- Voice clone thật: cả hai request HTTP trả `200`; output RIFF/WAVE hợp lệ.
  - VieNeu clone: 376,364 bytes.
  - Chatterbox clone: 314,960 bytes.

Clone artifacts nằm cùng thư mục `run`:

- `C:\Users\PC\AppData\Local\Temp\tts-cuda-e2e-report-20260826045434\run\clone-vi.wav`
- `C:\Users\PC\AppData\Local\Temp\tts-cuda-e2e-report-20260826045434\run\clone-en.wav`

## CLI sample flow

CLI dùng chính câu mẫu dự án và tách rõ hai phase `server` và `client_third_party`:

```powershell
.venv\Scripts\python.exe scripts\test_server_e2e.py `
  --base-url http://127.0.0.1:8136 `
  --admin-key $env:TTS_ADMIN_KEY `
  --languages vi en
```

Kết quả CLI thật: `failed_checks=[]`.

- `vi` → model `tts-vietnamese`, provider `vieneu`, voice `Minh Đức`.
- `en` → model `tts-multilingual`, provider `chatterbox`, voice `default`.
- Translation `en`: `This morning at 7:45, Minh cheerfully said, "If the weather is nice tomorrow, let's go for coffee, try the new pastry, and take a few photos by the riverside!"`
- Report: `C:\Users\PC\AppData\Local\Temp\tts-sample-cli-report-20260826052315\run-vi-en\report.json`

Artifact E2E đầy đủ nằm tại:

`C:\Users\PC\AppData\Local\Temp\tts-cuda-e2e-report-20260826045434\run\report.json`

Các cảnh báo còn lại là `FutureWarning` từ thư viện upstream và cảnh báo Hugging Face unauthenticated; không làm request thất bại. Khi vận hành production nên cấu hình `HF_TOKEN` nếu cần tải lại model, đồng thời giữ cache offline sau bootstrap.

## Voice matrix thật cho câu mẫu

Đã chạy `scripts/generate_sample_voice_matrix.py` qua HTTP server thật (`127.0.0.1:8138`), tách hai phase `server` và `client_third_party`. Kết quả:

- 24 bản dịch JSON (23 ngôn ngữ Chatterbox và tiếng Việt).
- 43/43 file WAV hợp lệ (23 Chatterbox + 20 preset VieNeu), HTTP 200, không có failure.
- Report: `C:\Users\PC\AppData\Local\Temp\tts-voice-matrix-report-20260826053753\matrix\matrix-report.json`.
- Audio và bản dịch nằm trong thư mục `C:\Users\PC\AppData\Local\Temp\tts-voice-matrix-report-20260826053753\matrix`.
- Bản sao artifact trong workspace: `D:\nhathao\codex\tool\tts-server\artifacts\sample-voice-matrix-20260826` (68 files, khoảng 40 MB).

Chatterbox hiện chỉ công bố voice ID mặc định `default` và không có catalog voice theo tên; vì vậy ID model-local này được dùng cho từng ngôn ngữ Chatterbox. VieNeu có 20 preset voice riêng cho `vi` và đã sinh đủ từng file. Không tự bịa ID voice Chatterbox vì sẽ làm sai capability của model.

## LAN bind không TLS (tùy chọn)

Để cho client trong LAN truy cập trực tiếp, có thể chạy:

```powershell
.\scripts\run_server.ps1 -HostAddress 0.0.0.0 -PortNumber 8000 -AllowInsecureLan
```

Chế độ này đặt `LAN_ONLY=true`: middleware chỉ cho loopback và các peer RFC1918 (`10/8`, `172.16/12`, `192.168/16`), đồng thời từ chối IP public. Bind không TLS chỉ được bật khi truyền rõ `-AllowInsecureLan`; bind IP public vẫn bị chặn. Đã smoke-test server thật với `0.0.0.0:8141`, `/health/live` trả HTTP 200.
