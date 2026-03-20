---
name: banana-previz-renderer
description: >
  Generate production images with Banana Pro style image APIs using the structured JSON from
  gemini-video-story-adapter. Use when the user already has analysis output (asset_library and
  storyboard_script) and wants a pure image-generation pipeline with built-in guardrails: pre-flight
  asset coverage validation, prompt safety hardening, child safety checks, global lighting injection,
  and storyboard asset-description replacement before any API call.
---

# Banana Previz Renderer

这个 skill 是带前置 guardrails 的纯生图执行层，不做剧情分析。

输入应来自上一个 skill（`gemini-video-story-adapter`）生成的结构化 JSON，至少包含：

- `asset_library`
- `storyboard_script`

## Workflow

### Phase 0 - Pre-flight Validation

在任何 API 调用前，脚本会先执行：

1. 资产覆盖检查：扫描 `storyboard_script` 中的 `@实体` 标记和 `referenced_assets`，如果存在未在 `asset_library` 定义的资产，立即报错终止。
2. 内容安全扫描：对最终 prompt 执行高风险词替换，并强制追加安全后缀。
3. 儿童安全检测：如果资产描述中命中儿童关键词，后续涉及该资产的 prompt 会自动注入儿童安全护栏。

### Phase 1 - Asset Generation

1. 从 `asset_library` 读取 `full_prompt_string` 或 `visual_anchor`。
2. 按资产类型追加结构化出图约束：
   - 角色：四视图、纯白底、单角色主体。
   - 道具：多角度四视图、纯白底、仅展示道具主体。
   - 场景：全景/细节/俯视/地标特写四视图。
3. 自动追加：
   - 全局光影质量基底
   - style descriptor / style preset
   - 内容安全后缀
   - 儿童护栏后缀（如命中）

### Phase 2 - Human Review

资产图生成后建议先审阅 `assets.generated.json` 和 `outputs/assets/*`，确认风格与角色一致性，再继续分镜阶段。

### Phase 3 - Storyboard Generation

1. 读取分镜 prompt。
2. 将其中的 `@实体` 标记替换为对应资产的 `full_prompt_string`。
3. 把资产图 `image_path` 作为 `inline_data` 参考图带入分镜请求。
4. 对替换后的 prompt 再执行安全扫描、光影注入和儿童护栏注入。
5. 调用 API 生成分镜图，并执行 1K 质量门。

## Guardrails

当前实现落地的是下面这些规则：

- 资产全覆盖：未定义的 `@实体` 或非法 `referenced_assets` 会在预检查阶段直接失败。
- Prompt 替换：分镜 prompt 中的 `@角色_XXX` / `@道具_XXX` / `@场景_XXX`，以及对应简写别名，会在提交前替换为资产完整描述。
- 内容安全：会替换血腥、真实灾难等高风险词，并始终追加 anatomy-safe 后缀。
- 交通/撞击场景：检测到碰撞语义时，额外追加软体物理/非真实冲击后缀。
- 儿童安全：如果资产描述命中儿童关键词，相关 prompt 会自动追加儿童表情、肢体、环境、服装和特效护栏。
- 全局光影：所有资产图和分镜图请求都会追加统一的亮度、色彩和细节基底。

## Required Config

必须提供：

- `YUNWU_API_TOKEN`

可选：

- `YUNWU_BASE_URL`（默认 `https://yunwu.ai`）
- `BANANA_IDENTITY_MAP_JSON`（覆盖默认共享 identity-map 路径）
- `--model`（默认 `gemini-3.1-flash-image-preview`）
- `--image-size`（默认 `1K`）
- `--identity-map-json`（角色/道具基础图映射，可选；不传时按共享路径查找）
- `--concurrency`（默认 `3`）
- `--max-retries`（默认 `2`）
- `--resolution-rule`（默认 `long-edge`）
- `--asset-id` / `--character` / `--shot-id`（定向重生成）
- `--force-rerun`（忽略历史成功结果，全量重跑所选 phase）

默认模型与画质：

- 模型：`gemini-3.1-flash-image-preview`
- 尺寸档位：`1K`
- 风格预设：`photoreal-hq`

默认画幅：

- 角色/道具/场景（assets）：`16:9`
- 分镜图（storyboard）：`9:16`

## Execution Discipline

- 脚本会在输出目录写 `.run_banana_pipeline.lock`，阻止同目录并发实例。
- 批量任务执行期间每 10 秒输出 heartbeat。
- 仅 `429/5xx` 会自动重试；`4xx` 和状态不明确错误不会自动重试。
- 默认恢复模式是 `failed_only`，只续跑明确可重试失败项。
- 定向重生成会强制刷新你点名的资产或镜头。

## Script

主脚本：[run_banana_pipeline.py](./scripts/run_banana_pipeline.py)

自然语言入口：[run_banana_command.py](./scripts/run_banana_command.py)

示例：

```bash
export YUNWU_API_TOKEN="your-token"
export YUNWU_BASE_URL="https://yunwu.ai"

# 1) 先生成资产图
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --output-dir ./outputs \
  --style photoreal-hq \
  --image-size 1K

# 2) 再生成分镜图
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase storyboard \
  --assets-json ./outputs/assets.generated.json \
  --output-dir ./outputs

# 3) 只重生成指定角色
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --output-dir ./outputs \
  --character Rumi,Jinu
```

## Output Files

- `assets.generated.json`
- `storyboard.generated.json`

输出 JSON 会记录：

- `image_path`
- 宽高与 `resolution_ok`
- `guardrails`
- `child_safety_guardrail`
- `resume_mode` / `target_asset_ids` / `target_shot_ids`
