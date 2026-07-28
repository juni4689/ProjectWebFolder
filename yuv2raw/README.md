# yuv2raw — YUV → RAW 일괄 변환기

폴더 안의 `.yuv` 파일을 **한 번에 전부** RAW 파일로 변환합니다.
**원본 파일은 절대 수정하지 않고**, 항상 별도의 출력 폴더에 새 파일을 만듭니다.

- 파이썬 3.6 이상만 있으면 동작합니다. 추가 설치 패키지 없음.
- `numpy` 가 설치돼 있으면 자동으로 빠른 경로를 사용합니다(같은 결과, 약 3배 빠름).
- 입력 20종 이상 / 출력 8종 지원, 8·10·12·16비트 지원.

---

## 1. 가장 빠른 사용법

### Windows

`convert_folder.bat` **위로 변환할 폴더를 끌어다 놓으면** 끝입니다.
(더블클릭한 뒤 폴더 경로를 입력해도 됩니다.)

결과는 `<입력폴더>\raw_out\` 에 생깁니다.

> Python 이 없다면 <https://www.python.org> 에서 설치하세요.
> 설치 화면의 **"Add Python to PATH"** 를 반드시 체크해야 합니다.

> **`convert_folder.bat` 을 고칠 때 주의:** 이 파일은 반드시
> **영문(ASCII)만, 줄 끝은 CRLF** 로 유지해야 합니다.
> cmd.exe 는 배치 파일을 콘솔 코드페이지(한국어 윈도우는 CP949)로 읽기 때문에,
> 한글 주석을 UTF-8 로 넣으면 글자가 깨지는 정도가 아니라 **줄 구분 자체가 무너져서**
> `'raw.py'은(는) 내부 또는 외부 명령... 이 아닙니다` 같은 오류가 쏟아집니다.

### 명령줄

```bash
# 폴더 전체를 RGB24 raw 로 변환 (결과: <폴더>/raw_out)
python yuv2raw.py /경로/yuv_폴더

# 출력 폴더를 따로 지정
python yuv2raw.py ./in -o ./out

# 하위 폴더까지 훑기
python yuv2raw.py ./in -r

# 무엇을 어떻게 변환할지 미리 확인만 (파일을 만들지 않음)
python yuv2raw.py ./in --dry-run
```

---

## 2. 결과물

입력 `clip_1920x1080_nv12.yuv` 하나당 두 개의 파일이 생깁니다.

| 파일 | 내용 |
|---|---|
| `clip_1920x1080_nv12_1920x1080_rgb24.raw` | 변환된 RAW 데이터 |
| `clip_1920x1080_nv12_1920x1080_rgb24.raw.json` | 해상도·포맷·프레임 수 등 메타데이터 |

출력 이름에 해상도와 포맷을 붙이는 이유는, 헤더가 없는 RAW 파일을 나중에 다시 열 때
설정을 잘못 넣어 그림이 깨지는 일을 막기 위해서입니다.
원본 이름 그대로(`clip_1920x1080_nv12.raw`)를 원하면 `--plain-name` 을 쓰세요.
사이드카 json 이 필요 없으면 `--no-sidecar` 를 쓰면 됩니다.

사이드카 예시:

```json
{
  "source": "clip_1920x1080_nv12.yuv",
  "source_format": "nv12",
  "source_bit_depth": 8,
  "width": 1920,
  "height": 1080,
  "frames": 30,
  "output_format": "rgb24",
  "output_bytes_per_frame": 6220800,
  "matrix": "bt709",
  "range": "limited",
  "chroma_upsample": "nearest"
}
```

---

## 3. "깨지지 않게" 하는 장치들

헤더가 없는 YUV/RAW 파일에서 그림이 깨지는 원인은 거의 전부
**해상도·포맷·비트수를 잘못 짚은 것**입니다. 이 도구는 그런 경우 깨진 결과물을
만드는 대신 **변환을 거부하고 이유를 알려줍니다.**

- 파일 크기가 `프레임 크기 × 정수` 가 아니면 오류. (해상도/포맷이 틀렸다는 신호)
- 4:2:0 인데 해상도가 홀수인 경우처럼 서브샘플링에 맞지 않으면 오류.
- 색 변환 결과는 항상 `0..max` 로 클리핑 — 오버플로로 생기는 알록달록한 노이즈 없음.
- 출력은 `.part` 임시 파일에 쓴 뒤 **크기를 검증하고 나서** 최종 이름으로 바꿉니다.
  변환 도중 중단되어도 반쪽짜리 `.raw` 가 남지 않습니다.
- 원본 파일은 읽기 전용(`rb`)으로만 엽니다.
- 출력 파일이 이미 있으면 기본적으로 건너뜁니다(`--overwrite` 로 덮어쓰기).
- 여러 입력의 출력 이름이 겹치면 변환하지 않고 알려줍니다.

색 변환의 정확도는 Pillow 의 YCbCr→RGB 결과와 대조해 검증했으며 최대 오차는 1 LSB 입니다.

---

## 4. 입력 포맷

이름과 해상도는 **파일 이름에서 자동으로 찾습니다**.
`clip_1920x1080_nv12.yuv` 처럼 되어 있으면 아무 옵션도 필요 없습니다.

- 이름에 해상도가 없으면 → 파일 크기로 추정합니다.
  후보가 여러 개면 추측하지 않고 `--size` 를 요구합니다.
- 이름에 포맷이 없으면 → `i420` 으로 가정하고 로그에 표시합니다.
  (4:2:0 계열은 파일 크기가 같아서 크기만으로는 구분할 수 없습니다.)

옵션으로 직접 지정하면 파일 이름보다 우선합니다.

```bash
python yuv2raw.py ./in --size 1920x1080 --format nv12
```

| 그룹 | 포맷 |
|---|---|
| 4:2:0 평면 | `i420`(=yu12, iyuv, yuv420p), `yv12` |
| 4:2:0 반평면 | `nv12`, `nv21` |
| 4:2:2 평면 | `i422`(=yuv422p), `yv16`, `nv16`, `nv61` |
| 4:2:2 패킹 | `yuyv`(=yuy2), `uyvy`, `yvyu`, `vyuy` |
| 4:4:4 평면 | `i444`(=yuv444p), `yv24` |
| 흑백 | `gray`(=y8, y800) |
| 10/16비트 반평면 | `p010`, `p016`, `p210` |

평면·반평면 포맷은 비트수 접미사를 붙일 수 있습니다:
`yuv420p10le`, `i420_10`, `nv12-16bit` … (리틀엔디안만 지원)

전체 목록은 `python yuv2raw.py --list-formats` 로 볼 수 있습니다.

---

## 5. 출력 포맷 (`--out-format`)

| 이름 | 내용 | 프레임당 크기 |
|---|---|---|
| `rgb24` (기본) | R,G,B 8비트 인터리브 | W×H×3 |
| `bgr24` | B,G,R 8비트 인터리브 (OpenCV·비트맵 계열) | W×H×3 |
| `rgb48le` / `bgr48le` | 16비트 리틀엔디안 인터리브 | W×H×6 |
| `gray8` / `gray16le` | 휘도만 | W×H (×2) |
| `yuv444` | 색공간 변환 없이 크로마만 풀어서 Y,U,V 인터리브 | W×H×3 (원본 비트수 유지) |
| `planar` | Y,U,V 평면 그대로 (NV12 → I420 처럼 정규화만) | 입력과 동일 |

- **화질 손실이 전혀 없어야 한다면** `--out-format yuv444` 또는 `planar` 을 쓰세요.
  이 둘은 색공간 변환을 하지 않고 샘플 값을 그대로 옮깁니다.
- `rgb24`/`bgr24` 는 YUV→RGB 변환이 들어가므로 되돌릴 수 없는 반올림이 생깁니다
  (일반적인 이미지 도구에서 바로 열어 보려면 이쪽이 편합니다).

---

## 6. 색 변환 옵션

| 옵션 | 값 | 기본 | 설명 |
|---|---|---|---|
| `--matrix` | `auto`, `bt601`, `bt709`, `bt2020` | `auto` | `auto` 는 세로 720 이상이면 BT.709, 아니면 BT.601 |
| `--range` | `limited`, `full` | `limited` | `limited` = TV 레인지(Y 16~235). 카메라/디코더 출력은 대개 이쪽 |
| `--chroma` | `nearest`, `bilinear` | `nearest` | 크로마 확대 방식. `bilinear` 이 부드럽지만 원본 샘플이 바뀝니다 |

**결과가 뿌옇거나 대비가 이상하면** `--range` 를 반대로 바꿔 보세요.
이것이 색이 "떠 보이는" 가장 흔한 원인입니다.

---

## 7. 전체 옵션

```
python yuv2raw.py [경로 ...] [옵션]

  -o, --output 폴더     출력 폴더 (기본: 입력 폴더 아래 raw_out)
      --size WxH        입력 해상도 (기본: 자동 판별)
      --format FMT      입력 YUV 포맷 (기본: 자동 판별)
      --out-format FMT  출력 RAW 포맷 (기본: rgb24)
      --matrix M        색변환 행렬 (기본: auto)
      --range R         limited | full (기본: limited)
      --chroma C        nearest | bilinear (기본: nearest)
  -r, --recursive       하위 폴더까지 훑기
      --pattern P       대상 파일 패턴, 쉼표로 여러 개 (기본: *.yuv)
      --frames N        파일당 최대 N 프레임만 변환
      --overwrite       기존 출력 파일 덮어쓰기 (기본: 건너뜀)
      --allow-partial   끝이 잘린 파일에서 온전한 프레임까지만 변환
      --plain-name      출력 이름을 <원본이름>.raw 로
      --no-sidecar      .json 사이드카를 만들지 않음
  -j, --jobs N          동시에 변환할 파일 수 (기본: auto)
      --dry-run         계획만 출력하고 파일은 만들지 않음
  -q, --quiet           로그 줄이기
      --list-formats    지원 포맷 목록 출력
```

---

## 8. 사용 예

```bash
# 10비트 4K 소스를 16비트 RGB 로
python yuv2raw.py ./in --format yuv420p10le --size 3840x2160 --out-format rgb48le

# 웹캠 YUYV 덤프를 BGR 로 (OpenCV 로 읽을 용도)
python yuv2raw.py ./cap --format yuyv --size 1280x720 --out-format bgr24

# 화질 손실 없이 NV12 를 I420 평면으로 정리
python yuv2raw.py ./in --out-format planar

# 확장자가 .yuv 가 아닌 파일들도 함께
python yuv2raw.py ./in --pattern "*.yuv,*.nv12,*.bin"

# 각 파일의 첫 프레임만 뽑아서 미리보기용으로
python yuv2raw.py ./in --frames 1
```

### 변환한 RAW 열어 보기

- **ImageJ / Fiji**: File → Import → Raw…, Image type 을 `24-bit RGB`,
  Width/Height 를 사이드카 json 값으로, Number of images 를 `frames` 로 지정.
- **Photoshop**: 열기에서 Photoshop Raw 선택 → Width/Height, Channels `3`,
  Depth `8 bit`, Header `0`, Interleaved 체크.
- **Python**:
  ```python
  import numpy as np, json
  info = json.load(open("out.raw.json", encoding="utf-8"))
  a = np.fromfile("out.raw", dtype=np.uint8)
  a = a.reshape(info["frames"], info["height"], info["width"], 3)
  ```

---

## 9. 문제 해결

| 메시지 / 증상 | 원인과 해결 |
|---|---|
| `파일 크기가 프레임 크기의 배수가 아닙니다` | 해상도나 포맷이 실제와 다릅니다. `--size` / `--format` 을 확인하세요. 끝이 잘린 파일이 확실하면 `--allow-partial`. |
| `해상도 후보가 여러 개라 자동 판별할 수 없습니다` | `--size 1920x1080` 처럼 직접 지정하세요. |
| `가로 해상도가 2의 배수여야 합니다` | 4:2:0/4:2:2 는 짝수 해상도만 가능합니다. 해상도를 다시 확인하세요. |
| 색이 뒤바뀜 (빨강↔파랑) | 입력이 `nv21`/`yv12` 인데 `nv12`/`i420` 로 읽었을 가능성. `--format` 을 바꿔 보세요. |
| 전체적으로 뿌옇거나 검정이 뜸 | `--range` 를 `full` 로 (또는 반대로) 바꿔 보세요. |
| HD 인데 색이 살짝 어긋남 | `--matrix bt709` (SD 소스라면 `bt601`) 로 지정하세요. |
| 세로로 줄무늬가 생김 | 반평면(NV12)을 평면(I420)으로 읽었을 때 나타납니다. `--format nv12`. |
| 변환이 느림 | `pip install numpy` (약 3배). 파일이 여러 개면 `-j 4` 로 병렬 처리. |

---

## 10. 테스트

```bash
python3 tests/test_yuv2raw.py            # numpy 경로
YUV2RAW_NO_NUMPY=1 python3 tests/test_yuv2raw.py   # 순수 파이썬 경로
```

50개 테스트가 포맷 해석, 색 정확도, 두 백엔드의 바이트 단위 일치,
잘린 파일 거부, 원본 미변경 등을 확인합니다.
