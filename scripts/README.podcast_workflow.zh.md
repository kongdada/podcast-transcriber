# 播客下载与转录工作流（中文说明）

本文档目标：让新用户 clone 项目后，按步骤执行，输入一个播客链接即可得到转录文件。

脚本入口：`scripts/podcast_workflow.py`

## 能力范围

脚本会自动完成以下流程：

1. 解析 Apple Podcasts / 小宇宙链接
2. 下载音频并转码为 MP3
3. 用本地 `whisper.cpp` 转写
4. （默认）用本地 `small.en-tdrz` 合并说话人标签
5. 输出 `01_transcript.md` 等结果文件

## 从零开始（可直接复制）

### 1) clone 并进入目录

```bash
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
```

### 2) 编译 `whisper-cli`

```bash
cmake -B build
cmake --build build -j --config Release
```

### 3) 安装依赖

macOS:

```bash
brew install yt-dlp ffmpeg
```

Ubuntu:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-pip
python3 -m pip install -U yt-dlp
```

### 4) 下载本地模型

下载主转写模型（高精度）：

```bash
./models/download-ggml-model.sh large-v3-turbo
```

下载说话人分离模型（默认开启）：

```bash
curl -fL "https://huggingface.co/akashmjn/tinydiarize-whisper.cpp/resolve/main/ggml-small.en-tdrz.bin" \
  -o ./models/ggml-small.en-tdrz.bin \
  || curl -fL "https://hf-mirror.com/akashmjn/tinydiarize-whisper.cpp/resolve/main/ggml-small.en-tdrz.bin" \
  -o ./models/ggml-small.en-tdrz.bin
```

### 5) 一条命令转录（输入链接）

```bash
python3 scripts/podcast_workflow.py --url "<podcast-episode-url>"
```

示例：

```bash
python3 scripts/podcast_workflow.py \
  --url "https://www.xiaoyuzhoufm.com/episode/69a64629de29766da93331ec"
```

## 输出结果

每次运行会生成一个新目录，例如：

```text
outputs/20260321-123456-某期标题/
  audio.mp3
  01_transcript.md
```

最常用结果文件：`01_transcript.md`

查看最近一次结果目录：

```bash
latest="$(ls -1dt outputs/* | head -n 1)"
echo "$latest/01_transcript.md"
```

如需保留调试产物（`json/srt/txt` 与 `run_manifest.json`），可加：

```bash
python3 scripts/podcast_workflow.py --url "<podcast-episode-url>" --keep-json-artifacts
```

## 节目页（多集）用法

节目页链接会交互列出最近 10 集。若在非交互环境（CI/脚本）运行，请显式指定集数：

```bash
python3 scripts/podcast_workflow.py \
  --url "<show-url>" \
  --episode-index 1
```

## 常用参数

- `--url`：必填，播客链接
- `--episode-index`：节目页选择第几集（1-based）
- `--out-root`：输出目录根路径（默认 `./outputs`）
- `--whisper-bin`：`whisper-cli` 路径（默认 `./build/bin/whisper-cli`）
- `--asr-model`：主转写模型路径（默认 `./models/ggml-large-v3-turbo.bin`）
- `--gpu` / `--no-gpu`：开启/关闭 GPU 加速（默认开启）
- `--keep-awake` / `--no-keep-awake`：运行时防休眠（macOS 默认开启）
- `--progress-interval`：进度心跳打印间隔秒数（默认 `30`）
- `--keep-json-artifacts` / `--no-keep-json-artifacts`：是否保留 `json/srt/txt` 与 `run_manifest`（默认不保留）
- `--diarization` / `--no-diarization`：开启/关闭说话人分离（默认开启）
- `--tdrz-model`：说话人分离模型路径（默认 `./models/ggml-small.en-tdrz.bin`）
- `--language`：转录语言（默认 `zh`）
- `--threads`：线程数（默认 `8`）

## 常见问题

1. 报错缺少 `ggml-small.en-tdrz.bin`
使用上面的 `curl` 命令下载，或临时使用 `--no-diarization`。

2. 节目页在非交互环境失败
加上 `--episode-index`。

3. 只想要纯转写，不要说话人分离
加 `--no-diarization` 即可。
