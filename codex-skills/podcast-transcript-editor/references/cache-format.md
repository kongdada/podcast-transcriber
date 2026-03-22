# 缓存与块级筛选

## 目标

减少 token 消耗，同时尽量不牺牲清洗质量。

## 默认缓存文件

清洗缓存默认写在 transcript 同目录：

```text
.podcast-transcript-editor-cache.json
```

## 缓存结构

```json
{
  "version": 1,
  "prompt_id": "cleanup-zh-v1-compact",
  "created_at": "2026-03-22T12:00:00+08:00",
  "updated_at": "2026-03-22T12:05:00+08:00",
  "entries": {
    "a1b2c3...": {
      "block_hash": "sha256...",
      "prompt_id": "cleanup-zh-v1-compact",
      "cleaned_block": "清洗后的完整块",
      "source_header": "张潇雨（00:00:00 - 00:00:10）：",
      "model": "gpt-4.1",
      "updated_at": "2026-03-22T12:05:00+08:00"
    }
  }
}
```

## 块级决策

规划器把正文块分成三类：

- `pass_through`
  - 直接沿用原文，不送模型
- `needs_model`
  - 送模型做忠实清洗
- `from_cache`
  - 复用缓存结果，不再送模型

## 直接跳过模型的块

满足下面条件时，优先 `pass_through`：

- 文本较短
- 标点已经正常
- 没有明显重复串
- 没有命中 profile 的术语误写或噪声短语
- 没有可疑噪声标记或长句异常

## 直接送模型的块

命中下面规则时，优先 `needs_model`：

- 重复短语明显
- 长段几乎没有句末标点
- 命中 profile 中的已知误写或噪声短语
- 混入明显噪声标记
- 行太长、标点密度过低、可读性明显差

## Prompt 版本控制

缓存 key 绑定 `prompt_id`。

只要下面任一项变化，就应视为缓存失效：

- 清洗 prompt 改了
- 块级规则改了
- 原始 block 文本改了
