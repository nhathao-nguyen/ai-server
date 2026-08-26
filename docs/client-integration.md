# Client integration guide

Tài liệu này mô tả contract HTTP hiện tại của `tts-server` để client, ứng dụng desktop hoặc bên thứ ba kết nối và gọi server.

## Cảnh báo quan trọng

- Không ghi API key thật vào source code, log, git hoặc tài liệu. Các ví dụ dùng `<API_KEY>`.
- Client phải đọc `/v1/models` để lấy model, language, voice và capability hiện tại; không hardcode catalog.
- `tts-vietnamese`/`vieneu` dùng cho tiếng Việt. `tts-multilingual`/`chatterbox` dùng cho các ngôn ngữ còn lại mà model công bố.
- Chatterbox hiện không có named voice catalog: voice mặc định là `default`; muốn đổi giọng phải dùng reference audio qua voice clone.
- VieNeu có voice preset riêng; không dùng voice của VieNeu cho Chatterbox hoặc ngược lại.
- `/health/live` có thể gọi từ LAN. `/health`, `/health/ready`, `/docs`, `/redoc` và `/openapi.json` chỉ dành cho loopback (`127.0.0.1`/`::1`).
- API key chỉ được trả đầy đủ một lần khi tạo. Client phải lưu ngay giá trị `key` trong response tạo key.

## Bắt đầu nhanh

### Địa chỉ và header

```text
BASE_URL=http://127.0.0.1:8000
Authorization: Bearer <API_KEY>
Accept: application/json
Content-Type: application/json
```

Nếu server bind LAN, thay `127.0.0.1` bằng IPv4 private của máy chủ, ví dụ `http://192.168.1.23:8000`.

Thiết lập biến cho các lệnh Bash/cURL bên dưới:

```bash
export BASE_URL="http://127.0.0.1:8000"
export API_KEY="<API_KEY>"
export ADMIN_KEY="<ADMIN_KEY>"
export PRODUCT_KEY="<PRODUCT_KEY>"
```

### Kiểm tra server

```bash
curl -i http://127.0.0.1:8000/health/live
curl -i -H "Authorization: Bearer $API_KEY" http://127.0.0.1:8000/v1/models
```

Kết quả thành công gồm `HTTP 200`. `/v1/models` trả đồng thời hai trường tương thích: `data` và `models`.

### Python client tối thiểu

Cài dependency ở máy client:

```bash
python -m pip install requests
```

```python
import os
import requests

BASE_URL = os.getenv("TTS_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ["TTS_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

live = requests.get(f"{BASE_URL}/health/live", timeout=10)
live.raise_for_status()
print(live.json())

models = requests.get(f"{BASE_URL}/v1/models", headers=HEADERS, timeout=60)
models.raise_for_status()
for model in models.json()["models"]:
    print(model["id"], model["provider"], model["available"])
```

### PowerShell client (Windows)

```powershell
$BaseUrl = "http://127.0.0.1:8000"
$ApiKey = $env:TTS_API_KEY
$Headers = @{ Authorization = "Bearer $ApiKey" }

Invoke-RestMethod "$BaseUrl/health/live"
$models = Invoke-RestMethod "$BaseUrl/v1/models" -Headers $Headers
$models.models | Select-Object id, provider, available
```

Gọi TTS và lưu WAV bằng PowerShell:

```powershell
$body = @{
  model = "tts-vietnamese"
  input = "Xin chào, đây là bản đọc thử."
  language = "vi"
  voice = "Adam"
  response_format = "wav"
  speed = 1.0
  options = @{}
} | ConvertTo-Json -Depth 5

Invoke-WebRequest "$BaseUrl/v1/audio/speech" -Method Post `
  -Headers $Headers -ContentType "application/json" -Body $body `
  -OutFile ".\output.wav"
```

## Xác thực và scopes

Mọi endpoint `/v1/*` có xác thực đều dùng:

```http
Authorization: Bearer ai_sk_...
```

Các scope hiện có:

| Scope | Dùng cho |
|---|---|
| `admin.full` | Quản trị key, model, runtime, backup, metrics, jobs và events |
| `llm.generate` | `/v1/chat/completions` |
| `llm.translate` | `/v1/translations` |
| `tts.generate` | `/v1/audio/speech`, `/v1/audio/preview` |
| `tts.clone` | `/v1/audio/voice-clone` |
| `usage.read` | `/v1/usage` |

`admin.full` không tự động thay thế `usage.read` cho `/v1/usage`. Product client nên dùng key riêng có đúng scope cần thiết.

### Tạo product key bằng admin API

Request:

```bash
curl -X POST "$BASE_URL/v1/admin/api-keys" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "my-client",
    "owner_note": "production client",
    "scopes": ["llm.translate", "tts.generate", "usage.read"],
    "rate_limit_per_minute": 60,
    "daily_quota_credits": 1000,
    "initial_credits": 1000
  }'
```

Các trường tùy chọn: `expires_at` (ISO-8601), `rate_limit_per_minute` (>=1), `daily_quota_credits` (>=0), `initial_credits` (>=0). Scope rỗng hoặc không hợp lệ trả `422`.

## Model và voice

### Khám phá capability động

```bash
curl "$BASE_URL/v1/models" -H "Authorization: Bearer $API_KEY"
```

Mỗi model có các trường chính: `id`, `provider`, `physical_model`, `available`, `status`, `lifecycle_state`, `supported_languages`, `supported_options`, `default_options`, `voice_mode`, `preset_voice_names`, `default_voice`, `supports_voice_clone`, `unsupported_features` và `runtime` (nếu có).

Catalog đã xác minh trong runtime hiện tại:

| Logical model | Provider | Language | Voice |
|---|---|---|---|
| `tts-vietnamese` | `vieneu` | `vi`, `vie` | 20 preset tiếng Việt; mặc định `Adam` |
| `tts-multilingual` | `chatterbox` | `ar`, `da`, `de`, `el`, `en`, `es`, `fi`, `fr`, `he`, `hi`, `it`, `ja`, `ko`, `ms`, `nl`, `no`, `pl`, `pt`, `ru`, `sv`, `sw`, `tr`, `zh` | `default`; không có named voice preset |
| `llm-default` | `ollama` | LLM | Không áp dụng |

Danh sách VieNeu hiện tại: `Minh Đức`, `Phạm Tuyên`, `Thái Sơn`, `Xuân Vĩnh`, `Thanh Bình`, `Trúc Ly`, `Ngọc Linh`, `Đoan Trang`, `Mai Anh`, `Thục Đoan`, `Minh Triết`, `Thùy Dung`, `Quang Sơn`, `Ngọc Trân`, `Mỹ Duyên`, `Quỳnh Anh`, `Đức Trí`, `Kim Thanh`, `Ngọc Huyền`, `Adam`.

Client nên dùng đúng `preset_voice_names` trả về từ server vì catalog có thể thay đổi theo manifest/runtime.

## Dịch văn bản

Endpoint: `POST /v1/translations` — cần scope `llm.translate`.

```bash
curl -X POST "$BASE_URL/v1/translations" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Sáng nay, lúc 7 giờ 45 phút, Minh vui vẻ nói: Nếu ngày mai trời đẹp, chúng ta sẽ đi uống cà phê nhé!",
    "source_language": "vi",
    "target_language": "en",
    "style": "neutral"
  }'
```

`style` chỉ nhận `neutral`, `formal`, `casual` hoặc `literal`. `source_language` có thể bỏ trống để dùng `auto`. Response:

```json
{
  "translation": "...",
  "target_language": "en",
  "model": "llm-default",
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

## Chat completion

Endpoint: `POST /v1/chat/completions` — cần scope `llm.generate`.

### Non-streaming

```bash
curl -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llm-default",
    "messages": [{"role": "user", "content": "Viết một câu chào ngắn bằng tiếng Việt."}],
    "stream": false,
    "temperature": 0.7,
    "max_tokens": 128,
    "user": "client-001"
  }'
```

`temperature` nằm trong `0..2`, `max_tokens` trong `1..32768`; request không nhận field ngoài schema.

### Streaming SSE

Đặt `"stream": true`. Server trả `Content-Type: text/event-stream`, mỗi phần có dạng `data: {...}\n\n`, cuối luồng là usage chunk và `data: [DONE]`.

```python
import requests

response = requests.post(
    f"{BASE_URL}/v1/chat/completions",
    headers={**HEADERS, "Accept": "text/event-stream"},
    json={
        "model": "llm-default",
        "messages": [{"role": "user", "content": "Xin chào"}],
        "stream": True,
    },
    stream=True,
    timeout=900,
)
response.raise_for_status()
for line in response.iter_lines(decode_unicode=True):
    if line:
        print(line)
```

## Text-to-speech

### Speech và preview

Hai endpoint dùng cùng schema và cần scope `tts.generate`:

- `POST /v1/audio/speech` — sinh audio chính thức.
- `POST /v1/audio/preview` — preview ngắn, text tối đa 500 ký tự.

Request tối thiểu:

```bash
curl -X POST "$BASE_URL/v1/audio/speech" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tts-vietnamese",
    "input": "Xin chào, đây là bản đọc thử.",
    "language": "vi",
    "voice": "Adam",
    "response_format": "wav",
    "speed": 1.0,
    "options": {}
  }' \
  --output output.wav
```

`input` hoặc `text` là bắt buộc; `response_format` hiện chỉ nhận `wav`; `speed` nhận `0.1 < speed <= 4.0`, nhưng capability provider hiện công bố `speed != 1.0` là unsupported, vì vậy client nên giữ `1.0`.

Ví dụ chọn model theo ngôn ngữ:

```json
{
  "model": "tts-multilingual",
  "input": "This is a multilingual test.",
  "language": "en",
  "voice": "default",
  "response_format": "wav",
  "speed": 1.0,
  "options": {}
}
```

Các option phải lấy từ `capabilities.supported_options`. Default hiện tại:

| Provider | Default options |
|---|---|
| Chatterbox | `exaggeration=0.5`, `cfg_weight=0.5`, `temperature=0.8`, `repetition_penalty=1.2`, `min_p=0.05`, `top_p=1.0` |
| VieNeu | `denoise=true`, `use_ref_codes=true`, `temperature=0.8`, `top_k=25`, `top_p=0.95`, `max_new_frames=300`, `repetition_penalty=1.2`, `repetition_window=64`, `max_chars=256`, `silence_p=0.15`, `crossfade_p=0.0`, `apply_watermark=true`, `batch_size=null` |

Response là bytes WAV. Client nên lưu các header metadata:

`X-TTS-Model`, `X-TTS-Provider`, `X-TTS-Characters`, `X-TTS-Duration-Ms`, `X-TTS-Generation-Ms`, `X-TTS-Credits`, `X-TTS-Voice`, `X-TTS-Effective-Options`, `X-TTS-Speed`.

### Voice clone

Endpoint: `POST /v1/audio/voice-clone` — cần scope `tts.clone`, dùng `multipart/form-data`.

```bash
curl -X POST "$BASE_URL/v1/audio/voice-clone" \
  -H "Authorization: Bearer $API_KEY" \
  -F "text=Đây là câu đọc bằng giọng tham chiếu." \
  -F "language=vi" \
  -F "model=tts-vietnamese" \
  -F "voice=Adam" \
  -F "speed=1.0" \
  -F 'options_json={"denoise":true}' \
  -F "reference_audio=@reference.wav" \
  --output cloned.wav
```

Field hỗ trợ: `text`, `language`, `model`, `voice`, `reference_transcript`, `speed`, `options_json`, `reference_audio`. Audio tham chiếu nhận `.wav`, `.mp3`, `.flac`, `.m4a`, `.ogg`; server tự xóa file tạm sau request. Giới hạn mặc định là 25 MiB và 300 giây, có thể thay đổi theo cấu hình server.

## Endpoint quản trị

Tất cả endpoint dưới đây cần scope `admin.full` và không nên gọi bằng product key:

| Method | Endpoint | Mục đích |
|---|---|---|
| `GET` | `/v1/admin/api-keys?include_inactive=true` | Liệt kê key metadata; không trả full key |
| `POST` | `/v1/admin/api-keys` | Tạo key; full key chỉ có trong response này |
| `POST` | `/v1/admin/api-keys/{key_id}/enable` | Bật key |
| `POST` | `/v1/admin/api-keys/{key_id}/disable` | Tắt key |
| `DELETE` | `/v1/admin/api-keys/{key_id}` | Revoke key |
| `DELETE` | `/v1/admin/api-keys/{key_id}/permanent` | Xóa vĩnh viễn; key phải không còn active và không có history |
| `GET` | `/v1/admin/scopes` | Đọc danh sách scope từ server |
| `GET` | `/v1/admin/overview` | Model, GPU và scheduler |
| `GET` | `/v1/admin/models` | Model catalog quản trị |
| `GET` | `/v1/admin/gpu` | GPU status |
| `GET` | `/v1/admin/build-info` | Build/source/manifest digests |
| `GET` | `/v1/admin/usage` | Usage theo key, filter `start`, `end`, `provider`, `model`, `status` |
| `GET` | `/v1/admin/metrics` | Latency, success/error rate, memory và scheduler |
| `POST` | `/v1/admin/backup` | Backup SQLite; trả filename |
| `POST` | `/v1/admin/runtime/reset` | Reset runtime, giữ key/usage/credits/model cache |
| `GET` | `/v1/admin/jobs` | Job và scheduler state |
| `GET` | `/v1/admin/events` | Event buffer |
| `GET` | `/v1/admin/events/stream?after=<sequence>` | Event SSE realtime |

Ví dụ đọc overview:

```bash
curl "$BASE_URL/v1/admin/overview" -H "Authorization: Bearer $ADMIN_KEY"
```

## Usage của product client

Endpoint `GET /v1/usage` cần `usage.read` và chỉ trả các event thuộc key đang dùng:

```bash
curl "$BASE_URL/v1/usage?limit=50" \
  -H "Authorization: Bearer $PRODUCT_KEY"
```

`limit` nhận `1..200`; response có `total_events` và `events`.

## Lỗi và retry

Lỗi API có format thống nhất:

```json
{
  "error": {
    "code": "scope_required",
    "message": "Scope 'tts.generate' is required",
    "details": {"scope": "tts.generate"}
  }
}
```

Các status thường gặp:

| HTTP | Ý nghĩa | Cách xử lý |
|---:|---|---|
| `400` | Request không hợp lệ ở HTTP layer | Kiểm tra body/header |
| `401` | Thiếu hoặc sai bearer key | Kiểm tra `Authorization` |
| `403` | Key thiếu scope, bị disable hoặc revoke | Dùng key đúng scope; không retry mù |
| `409` | Xung đột trạng thái, ví dụ xóa key đang active | Disable/revoke trước theo response |
| `413` | Audio upload quá lớn | Giảm kích thước hoặc thời lượng |
| `422` | Schema, language, option hoặc voice không hợp lệ | Đọc `error.details`, đối chiếu `/v1/models` |
| `429` | Rate limit/quota | Tôn trọng `Retry-After` nếu có và backoff |
| `500` | Lỗi runtime/provider | Ghi request context, kiểm tra `/health/ready` và admin metrics |

Không retry tự động các lỗi `401`, `403`, `409`, `422`. Với `429` hoặc lỗi mạng tạm thời, dùng exponential backoff có giới hạn; với TTS/LLM dài nên timeout tối đa khoảng 900 giây.

## Chạy server cho client LAN

### Loopback-only

```powershell
.\scripts\run_server.ps1 -HostAddress 127.0.0.1 -PortNumber 8000
```

### LAN private, không TLS theo yêu cầu nội bộ

```powershell
.\scripts\run_server.ps1 -HostAddress 0.0.0.0 -PortNumber 8000 -AllowInsecureLan
```

Server chỉ chấp nhận loopback hoặc RFC1918 private IPv4 (`10/8`, `172.16/12`, `192.168/16`); public IP bind bị từ chối. Client trong cùng LAN gọi `http://<LAN_IP>:8000`. Client ngoài LAN không nằm trong phạm vi hỗ trợ.

### Test từ client LAN

```bash
curl -i http://192.168.1.23:8000/health/live
curl -i -H "Authorization: Bearer $API_KEY" http://192.168.1.23:8000/v1/models
```

Nếu không kết nối được, kiểm tra server log đang có dòng `Uvicorn running on http://0.0.0.0:8000`, Windows Firewall và địa chỉ IPv4 private thực tế của máy chủ.

## Kiểm tra end-to-end khuyến nghị

1. Gọi `/health/live` không key.
2. Gọi `/v1/models` bằng product key và chọn model/language/voice từ response.
3. Gọi `/v1/translations` nếu client cần dịch.
4. Gọi `/v1/audio/speech`, lưu WAV và kiểm tra các `X-TTS-*` headers.
5. Gọi `/v1/usage` bằng key có `usage.read` để kiểm tra credits/event.
6. Chỉ dùng admin key cho `/v1/admin/*`; kiểm tra `/v1/admin/metrics` sau các request thật.

Runtime hiện tại đã được xác minh bằng luồng HTTP thật: server `8000` trả health/models `200`, ba model khả dụng, và bind LAN tạm `0.0.0.0:8147` đã trả health `200` trên các IPv4 private trước khi được dừng và đóng cổng.
