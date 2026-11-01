# JellyDub Engine

**Tạo thuyết minh tiếng Việt từ phụ đề, đồng bộ với phim.**

Engine Python độc lập dành cho xử lý file, phát trực tiếp và tích hợp Jellyfin/TinyTV. Nhận phụ đề SRT tiếng Anh hoặc tiếng Việt, tạo giọng đọc, căn thời gian rồi trộn/xuất âm thanh. File phim gốc được giữ nguyên.

`SRT → Dịch tiếng Việt → Tổng hợp giọng nói → Căn thời gian → Trộn / Xuất / Stream`

## Điểm nổi bật

- **Ba cách dùng:** CLI xử lý file, HTTP API tạo giọng theo vị trí phát, HLS thử nghiệm cho Jellyfin.
- **Linh hoạt backend:** OPUS-MT hoặc Gemini để dịch; Edge-TTS hoặc VieNeu để đọc.
- **Tái sử dụng cache:** lưu bản dịch và từng đoạn giọng nói, tiếp tục công việc khi chạy lại.
- **Tìm phụ đề:** hỗ trợ file cạnh phim, phụ đề nhúng và nguồn trực tuyến tùy chọn.

## Bắt đầu nhanh

Yêu cầu **Python 3.11–3.13**, **FFmpeg** và [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Ubuntu / Debian
sudo apt install ffmpeg

git clone https://github.com/tecix/jellydub-engine.git
cd jellydub-engine
uv sync
uv run jellydub doctor

# Tạo thuyết minh từ phụ đề tiếng Việt
uv run jellydub dub movie.mkv --srt movie.vi.srt \
  --source-lang vi --out-dir ./dubbed
```

Mặc định dùng giọng `vi-VN-HoaiMyNeural` qua Edge-TTS, cần internet để tạo đoạn đọc chưa có trong cache. Có thể đổi sang `--voice vi-VN-NamMinhNeural`.

### Dịch phụ đề tiếng Anh

```bash
uv sync --extra translate
uv run jellydub translate movie.mkv --srt movie.en.srt --out-dir ./dubbed

# Kiểm tra/chỉnh bản dịch trước khi tạo giọng
uv run jellydub dub movie.mkv --srt dubbed/movie.vi.srt --out-dir ./dubbed
```

OPUS-MT chạy cục bộ sau lần tải model đầu tiên. Để dùng Gemini, đặt biến môi trường `GEMINI_API_KEY` rồi thêm `--translator gemini` vào lệnh dịch. Phụ đề tiếng Việt bỏ qua bước dịch; dùng `--source-lang vi` nếu nhận diện tự động chưa đúng.

## Chọn cách sử dụng

### 1. Tạo file thuyết minh

```bash
# Hạ tiếng gốc khi có lời thuyết minh
uv run jellydub dub movie.mkv --srt movie.vi.srt --mix voiceover --out-dir ./dubbed

# Tự tìm phụ đề trong máy, không tìm trên mạng
uv run jellydub dub movie.mkv --no-download --out-dir ./dubbed
```

| Chế độ trộn | Kết quả |
| --- | --- |
| `dub-only` — mặc định khi xử lý file | Chỉ giọng thuyết minh |
| `voiceover` | Giọng thuyết minh + tiếng gốc được hạ âm lượng |
| `replace` | Tách lời gốc, giữ nhạc/hiệu ứng; cần extra `separate` |

Có thể tải phụ đề trực tuyến bằng extra `opensubtitles`. Xem cấu hình mẫu tại [jellydub.example.toml](jellydub.example.toml) và hướng dẫn [tách nền âm thanh](docs/separation.md).

### 2. Live API cho ứng dụng phát video

```bash
uv run jellydub serve --port 8765
# Dịch bằng Gemini:
uv run jellydub serve --port 8765 --translator gemini
```

Client gửi SRT, cập nhật vị trí phát và lấy các đoạn WAV đã tạo. Engine ưu tiên đoạn quanh vị trí đang xem và điều chỉnh khi tua.

| Endpoint | Chức năng |
| --- | --- |
| `GET /v1/health` | Kiểm tra server |
| `POST /v1/sessions` | Tạo phiên với JSON gồm `srt`, `source_lang`, `title` |
| `GET /v1/sessions/{id}/cues?from=0&to=90` | Lấy trạng thái các câu trong khoảng thời gian (giây) |
| `GET /v1/sessions/{id}/audio/{n}.wav` | Lấy đoạn giọng đọc |
| `DELETE /v1/sessions/{id}` | Đóng phiên |

Xem thêm [hướng dẫn live bằng tiếng Việt](docs/live-dubbing-vi.md). Cài extra `live` nếu cần quảng bá server qua mDNS cho TinyTV.

### 3. HLS cho Jellyfin · Thử nghiệm

```bash
uv run jellydub stream movie.mkv --srt movie.vi.srt \
  --public-url http://192.168.1.10:8766 \
  --strm './library/Movie Vietnamese.strm'
```

Thay IP bằng địa chỉ LAN của máy chạy engine. Đặt `.strm` trong thư viện Jellyfin, quét lại thư viện và giữ lệnh chạy khi xem. Jellyfin và thiết bị phát phải truy cập được địa chỉ này.

HLS tạo segment theo yêu cầu, hỗ trợ tua và tái sử dụng cache. Hiện xuất **H.264 SDR tối đa 1080p/30fps + AAC stereo**; nguồn là file video cục bộ. Độ trễ phụ thuộc CPU, dịch và TTS; khả năng phát/tua cần kiểm tra trên thiết bị thực tế.

## Backend và cấu hình

| Thành phần | Lựa chọn | Ghi chú |
| --- | --- | --- |
| Dịch | `opus-mt` — mặc định | Cục bộ; cài extra `translate` |
| Dịch | `gemini` | API trực tuyến; cần API key và quota |
| Dịch | `agy`, `agy-stream` | Thử nghiệm; cần Antigravity CLI đã đăng nhập |
| Giọng đọc | `edge` — mặc định | Trực tuyến; client không chính thức, không clone giọng |
| Giọng đọc | `vieneu` | Cục bộ; cài extra `vieneu`, hỗ trợ giọng tham chiếu |

```bash
# Cài các thành phần tùy chọn cần dùng
uv sync --extra translate --extra vieneu --extra separate

# Xem toàn bộ tùy chọn
uv run jellydub --help
uv run jellydub dub --help
uv run jellydub stream --help
```

Các lệnh hỗ trợ `--config jellydub.example.toml`; tham số CLI được ưu tiên. Giữ API key trong biến môi trường. Cache mặc định nằm tại `~/.cache/jellydub`.

> **Lưu ý:** Edge-TTS gửi văn bản tới Microsoft; Gemini và các backend Antigravity gửi văn bản tới Google. Live/HLS chưa có xác thực, chỉ nên dùng trong LAN hoặc tailnet tin cậy. Câu quá dài có thể cần rút gọn để khớp thời gian; engine chưa tự nhận diện người nói.

## Roadmap

Các phase tiếp theo là định hướng, chưa cam kết ngày phát hành.

| Phase | Trọng tâm | Mốc hoàn thành |
| --- | --- | --- |
| **0 · Độc lập** — hiện tại | Package, CLI, live API, HLS thử nghiệm, tài liệu | Cài đặt độc lập; bộ test offline chạy qua |
| **1 · Phát hành** | CI, phiên bản phát hành, chuẩn hóa API/cấu hình | Build wheel tái lập; chính sách tương thích rõ ràng |
| **2 · Streaming ổn định** | Hủy tác vụ khi tua, giới hạn hàng đợi/cache, retry | Đo và kiểm chứng thời gian mở phim/tua trên Jellyfin và Android TV |
| **3 · Chất lượng** | Bộ đánh giá bản dịch, phát âm, gán giọng nhân vật | So sánh chất lượng và độ trễ có thể tái lập |
| **4 · Triển khai** | Container, API có xác thực, ví dụ tích hợp | Triển khai và kết nối client theo tài liệu |
| **5 · Mở rộng** | Hàng đợi bền vững, metrics, giới hạn tài nguyên, GPU worker | Kiểm thử tải và phục hồi; công bố năng lực xử lý |

## Phát triển

```bash
uv sync --group dev
uv run pytest -q
uv build
```

Mã nguồn nằm trong `src/jellydub`; `pipeline.py` điều phối xử lý file, `live/` phục vụ API và HLS. Tên package/lệnh vẫn là `jellydub`. API Python chưa ổn định; ưu tiên CLI hoặc HTTP API khi tích hợp.

Test mặc định bỏ qua nhóm `e2e` và `slow` cần mạng/model. Không commit thông tin đăng nhập, phim, model hay cache.

**Giấy phép:** mã nguồn công bố theo MIT; thư viện, model và dịch vụ có điều khoản riêng. Xem [ghi chú giấy phép bên thứ ba](LICENSES-THIRD-PARTY.md).
