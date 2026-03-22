# 本地播客转录工作流

把 Apple Podcasts / 小宇宙链接转成可读的播客转录稿。

这个仓库现在有两层能力：

- `CLI`：本地下载音频、转 MP3、Whisper 转录、说话人轮次排版、硬规则清洗
- `Skill`：在 Codex 里对 `01_transcript.md` 做忠实语义清洗，生成 `02_transcript_clean.md`

## 快速入口

- 中文 CLI 说明：[`scripts/README.podcast_workflow.zh.md`](scripts/README.podcast_workflow.zh.md)
- Skill 定义：[`codex-skills/podcast-transcript-editor/SKILL.md`](codex-skills/podcast-transcript-editor/SKILL.md)
- `whisper.cpp` 原始构建/能力文档：[`README.whispercpp.md`](README.whispercpp.md)

## 项目能做什么

输入一个播客链接，CLI 会完成：

1. 解析 Apple Podcasts / 小宇宙单集或节目页链接
2. 下载音频并转为 `audio.mp3`
3. 调用本地 `whisper-cli` 生成 `01_transcript.json`
4. 可选调用本地 `tinydiarize` 生成说话人分离 JSON
5. 把转录结果整理成按对话轮次排版的 `01_transcript.md`
6. 按 `profile` 应用说话人名称、术语替换和噪声短语过滤

说明：

- 当前工作流按 `Speaker A / Speaker B` 两位对谈场景设计
- 本地 `tinydiarize` 主要提供二人轮次提示

如果你在 Codex 中使用 skill，还可以继续把 `01_transcript.md` 清洗为更顺畅、但仍忠实原文的 `02_transcript_clean.md`。

## 完整工作流程图

```mermaid
flowchart TD
    A["输入播客 URL<br/>Apple Podcasts / 小宇宙"] --> B["CLI: 解析 URL<br/>节目页则选择单集"]
    B --> C["yt-dlp 下载源音频"]
    C --> D["ffmpeg 转码<br/>输出 audio.mp3"]
    D --> E["whisper-cli 主转写<br/>输出 01_transcript.json"]
    E --> F{"开启 diarization?"}
    F -- "是" --> G["tinydiarize 二次转写<br/>输出 01_diarization_tdrz.json"]
    F -- "否" --> H["直接进入排版"]
    G --> H["合并说话人轮次 + profile 硬规则清洗<br/>生成 01_transcript.md"]
    H --> I["基础产物目录<br/>audio.mp3 / 01_transcript.md / JSON"]

    I --> J{"是否使用 Codex Skill<br/>做忠实语义清洗?"}
    J -- "否" --> K["结束<br/>交付 01_transcript.md"]
    J -- "是" --> L["Skill 读取 01_transcript.md<br/>必要时参考 JSON"]

    L --> M["cleanup_helper.py plan<br/>按块切分 transcript"]
    M --> N["块级脏块筛选"]
    N --> O{"块类型"}
    O -- "pass_through" --> P["直接沿用原文块"]
    O -- "from_cache" --> Q["复用 .podcast-transcript-editor-cache.json"]
    O -- "needs_model" --> R["用固定低浪费 prompt<br/>只清洗脏块"]

    P --> S["assemble<br/>组装最终清洗稿"]
    Q --> S
    R --> T["回填 cleaned_block 到 plan"]
    T --> S

    S --> U["输出 02_transcript_clean.md"]
    S --> V["更新块级缓存"]
```

## 一条命令得到转录稿

### 1. clone 仓库

```bash
git clone https://github.com/kongdada/podcast-transcriber.git
cd podcast-transcriber
```

### 2. 编译 `whisper-cli`

```bash
cmake -B build
cmake --build build -j --config Release
```

### 3. 安装依赖

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

### 4. 下载模型

主转写模型：

```bash
./models/download-ggml-model.sh large-v3-turbo
```

说话人分离模型：

```bash
curl -fL "https://huggingface.co/akashmjn/tinydiarize-whisper.cpp/resolve/main/ggml-small.en-tdrz.bin" \
  -o ./models/ggml-small.en-tdrz.bin \
  || curl -fL "https://hf-mirror.com/akashmjn/tinydiarize-whisper.cpp/resolve/main/ggml-small.en-tdrz.bin" \
  -o ./models/ggml-small.en-tdrz.bin
```

### 5. 运行脚本

```bash
python3 scripts/podcast_workflow.py --url "<podcast-url>"
```

示例：

```bash
python3 scripts/podcast_workflow.py \
  --url "https://www.xiaoyuzhoufm.com/episode/69a64629de29766da93331ec"
```

## 默认输出

每次运行会创建一个新的 `outputs/<run-id>/` 目录，默认保留：

```text
outputs/20260321-123456-某期标题/
  audio.mp3
  01_transcript.md
  01_transcript.json
  01_diarization_tdrz.json   # 仅在开启 diarization 时生成
```

默认不生成：

- `01_transcript.srt`
- `01_transcript.txt`
- `02_transcript_clean.md`

说明：

- `01_transcript.md` 是基础标准稿，适合直接阅读
- `01_transcript.json` 和说话人分离 JSON 会默认保留，供 skill 或人工复核复用
- `run_manifest.json` 只在 `--keep-json-artifacts` 时保留

## 对谈稿格式

`01_transcript.md` 默认会：

- 合并连续同一说话人的片段
- 说话人切换时空一行
- 长段按标点智能断行
- 单行目标长度控制在约 `100` 字

如果已知说话人名字，可直接覆盖 `Speaker A/B`：

```bash
python3 scripts/podcast_workflow.py \
  --url "https://www.xiaoyuzhoufm.com/episode/69a64629de29766da93331ec" \
  --speaker-a-name "张潇雨" \
  --speaker-b-name "雨白"
```

## Skill：忠实语义清洗

Skill 定义保存在仓库里：

- [`codex-skills/podcast-transcript-editor`](codex-skills/podcast-transcript-editor)

安装到本机 Codex skills 目录：

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/codex-skills/podcast-transcript-editor" ~/.codex/skills/podcast-transcript-editor
```

这个 skill 支持两种输入：

- 直接给播客 URL：先调用本地 CLI 生成 `01_transcript.md`，再做忠实清洗
- 直接给现有 `01_transcript.md`：只生成 `02_transcript_clean.md`

为了节省 token，skill 不会默认把整篇 transcript 都送进模型，而是：

- 先按块做脏块筛选
- 干净块直接跳过模型
- 命中缓存的块直接复用
- 只把 `needs_model` 的块送模型

Skill 的清洗边界：

- 忠实原文，不总结、不扩写、不改观点
- 只修明显错字、专有名词错误、标点、断句和轻微语病
- 保留口语感，不改写成文章

## profile 机制

`scripts/podcast_profiles/` 下的 JSON 配置可以固化节目级规则：

- `speaker_a_name`
- `speaker_b_name`
- `noise_phrases`
- `replacements`
- URL / 标题匹配规则

参考：

- [`scripts/podcast_profiles/README.md`](scripts/podcast_profiles/README.md)
- [`scripts/podcast_profiles/_template.profile.json`](scripts/podcast_profiles/_template.profile.json)

## 常见命令

节目页非交互指定集数：

```bash
python3 scripts/podcast_workflow.py \
  --url "<show-url>" \
  --episode-index 1
```

关闭说话人分离：

```bash
python3 scripts/podcast_workflow.py --url "<podcast-url>" --no-diarization
```

保留 `run_manifest.json`：

```bash
python3 scripts/podcast_workflow.py --url "<podcast-url>" --keep-json-artifacts
```

## 依赖与排错

脚本会在启动时检查：

- `yt-dlp`
- `ffmpeg`
- `whisper-cli`
- ASR 模型文件
- `tinydiarize` 模型文件（当开启 diarization 时）

常见问题：

1. 缺少 `ggml-small.en-tdrz.bin`
   按上面的 `curl` 命令下载，或临时使用 `--no-diarization`
2. 节目页在非交互环境失败
   显式加 `--episode-index`
3. 想要更顺畅、但忠实原文的清洗版
   使用仓库内的 `podcast-transcript-editor` skill

## 相关文档

- CLI 详细说明：[`scripts/README.podcast_workflow.zh.md`](scripts/README.podcast_workflow.zh.md)
- Skill 说明：[`codex-skills/podcast-transcript-editor/SKILL.md`](codex-skills/podcast-transcript-editor/SKILL.md)
- 归档的 `whisper.cpp` 文档：[`README.whispercpp.md`](README.whispercpp.md)
- CLI 详细说明：[`scripts/README.podcast_workflow.zh.md`](scripts/README.podcast_workflow.zh.md)
- Skill 说明：[`codex-skills/podcast-transcript-editor/SKILL.md`](codex-skills/podcast-transcript-editor/SKILL.md)
- 归档的 `whisper.cpp` 文档：[`README.whispercpp.md`](README.whispercpp.md)
