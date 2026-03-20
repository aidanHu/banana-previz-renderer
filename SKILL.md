---
name: banana-previz-renderer
description: Generate production images with Banana Pro style image APIs using the structured JSON from gemini-video-story-adapter. Use when the user already has analysis output (asset_library and storyboard_script) and wants a pure image-generation pipeline with built-in iron-law guardrails: enforces asset coverage, content safety, child safety, global lighting quality, and visual consistency before any API call is made. Generates 角色/道具/场景图 first, runs pre-flight validation, then generates 分镜图 with asset references and 1k resolution quality gate.
---

# Banana Previz Renderer

这个 skill 是**带前置铁律校验的纯生图执行层**，不做剧情分析。

输入应来自上一个 skill（`gemini-video-story-adapter`）生成的结构化 JSON，至少包含：

- `asset_library`
- `storyboard_script`

在任何 API 调用发出前，渲染器会对输入 JSON 执行**五条铁律**的前置校验与提示词加固（详见 [Guardrails / Iron Laws](#guardrails--iron-laws) 小节）。

## Workflow

### Phase 0 — Pre-flight Validation（铁律前置校验，每次运行必经）

在生成任何图片前，按顺序执行以下门控检查：

1. **资产覆盖检查（Iron Law 1）**：解析 `storyboard_script` 中每一帧的提示词，提取所有实体名词，与 `asset_library` 的 `asset_tag` 列表对比。若存在未定义的实体，**立即报错并终止**，提示用户补充资产定义或修正分镜提示词后重跑。
2. **内容安全扫描（Iron Law 2）**：对所有待提交提示词进行黑名单扫描，命中高危词时执行自动替换（见铁律二细则）。
3. **儿童护栏触发检测（Iron Law 3）**：扫描 `asset_library` 的 `asset_tag` 和描述，若匹配儿童关键词，设置 `child_safety_guardrail = True`，后续所有涉及该角色的提示词强制注入六维修饰语。

通过全部门控后进入生成阶段。

### Phase 1 — Asset Generation（资产图生成）

1. 读取分析结果 JSON。
2. 按铁律五的视觉规范，为每个资产类型（角色/道具/场景）组装结构化提示词，并在末尾物理注入 **全局 Style Descriptor** 和**光影质量基底**（Iron Law 4 & 5）。
3. 对每条提示词执行 Iron Law 2 的安全后缀拼接和 Iron Law 3 的儿童护栏注入（如已触发）。

角色基图参考（如 Rumi/Jinu/Mira）：

- 不需要先转 URL。
- 直接提供映射 JSON（可混用本地路径和 URL，示例见 [identity-map.example.json](./assets/identity-map.example.json)）。
- 脚本会在对应资产生成请求里自动附加这些参考图。

引用链路（Gemini 原生）：

- 资产阶段会把返回的内联图片保存为本地文件（`image_path`）。
- 分镜阶段读取这些 `image_path`，转为 `inline_data` 作为参考图输入。
- 这个流程不依赖公网 URL。

### Phase 2 — Human Review（人工确认，可选但推荐）

资产图生成后暂停，等待人工审核 `assets.generated.json`，确认视觉一致性后再执行分镜阶段。

### Phase 3 — Storyboard Generation（分镜图生成）

1. 读取已确认的资产 JSON，将分镜提示词中的 `@角色名` / `@道具名` / `@场景名` 标识符**物理替换**为对应的 `full_prompt_string`（Iron Law 1 的描述持久化要求）。
2. 对替换后的完整提示词再次执行 Iron Law 2 安全扫描、Iron Law 3 儿童护栏注入、Iron Law 4 光影基底注入。
3. 调用 API 生成分镜图，执行 1K 质量门检查。

---

## Guardrails / Iron Laws

> 这五条铁律通过**正则过滤、字符串强替换、必选 prompt 后缀拼接**三种工程手段在渲染管道中实体化，不依赖模型自觉遵守。

### Iron Law 1 — 全局视觉定义与资产全覆盖（无定义，不生成）

**前置门控**：

- 解析所有帧提示词（`first_frame_prompt` / `scela_prompt`），提取实体名词列表。
- 对比 `asset_library` 的 `asset_tag`。如有未定义实体，**报错拦截，不生成**。

**描述持久化**：

- 分镜提示词中的 `@实体名` 在提交 API 前必须被物理替换为该资产的 `full_prompt_string`。
- 禁止使用代词、”同上”或占位符。每一帧提示词必须是自治的全量描述。

### Iron Law 2 — YouTube 令人震惊与生理不适内容防护（安全底线）

**黑名单替换**（对最终提示词执行）：

- 极度血腥、断肢、真实城市毁灭等词汇 → 替换为温和/超现实描述（如”夸张的软体物理跌倒”）。
- 真实灾难敏感词 → 替换为”科幻/奇幻”环境描述。

**强制安全后缀**（所有生成请求硬编码追加）：

- Body Horror 防护：
  ```
  Ensure anatomically correct human proportions, natural facial features, no multiple limbs, no melted flesh, visually pleasing and safe aesthetic.
  ```
- 交通/撞击场景附加：
  ```
  surreal representation, soft body physics, jelly car physics, crash test dummy aesthetic, exaggerated cartoon physics, non-realistic impact, safe simulation
  ```

### Iron Law 3 — 儿童安全护栏与畸变自查

**触发器**：扫描 `asset_library` 的 `asset_tag` / 描述，匹配”儿童/小孩/男孩/女孩/幼儿”等关键词时，设置 `child_safety_guardrail = True`。

**六维强制注入**（涉及儿童角色的每次 API 请求必须追加）：

| 维度 | 强制注入内容 |
|------|------------|
| 表情 | `natural positive facial expression, bright clear eyes, no distorted facial features, no scary grimaces, calming and pleasant look` |
| 肢体 | `anatomically correct child limbs, accurate number of fingers and toes, natural posture, no broken bone physics` |
| 环境 | `brightly lit clean environment, vibrant colors, clear visibility, presence of adult supervision context (e.g. blurry adult figure in background), no dark scary corners` |
| 道具 | 预处理层将危险道具（刀/枪）替换为”glowing foam sword” / “brightly colored plastic water gun” |
| 服装 | `properly fitted modest clothing, fully covering torso, comfortable kid's apparel, non-revealing` |
| 剧情/特效 | `highly exaggerated magical effects, cartoonish dream-like action, safe and whimsical movement, soft colorful particles` |

### Iron Law 4 — 全局光影与画质底座（拒绝灰暗，强制高配质感）

所有生成请求（资产图 + 分镜图）**自动拼接**以下基底描述：

```
abundant natural light, bright and clear lighting, vibrant and rich colors, highly detailed and rich scene content, exquisite and nuanced character expressions and subtle fluid movements
```

### Iron Law 5 — 视觉一致性控制与资产结构化制图

**全局 Style Descriptor**：在适配器阶段定义，物理追加到所有提示词末尾，严禁省略或使用占位符。

**资产提示词组装规范**：

- **角色**：`[视觉描述] + 强制四视图在同一张图片上(正面、侧面、背面、正面半身特写)，纯白底，无背景，单人，严禁出现其他人物，图片左上角标注”@角色名”，文字与视图互不重叠，正面半身特写突出脸部细节 + [Style Descriptor]`
- **道具**：`[视觉描述] + 强制多角度四视图在同一张图片上(正视、侧视、俯视、正面特写细节)，纯白底，无背景，严禁出现角色肢体/手部/未定义场景元素，仅展示道具主体，产品特写 + [Style Descriptor]`
- **场景**：`[视觉描述] + 强制环境设计四视图在同一张图片上(全景、局部细节、俯视平面、核心地标特写)，纯白底背景展示设计稿，无干扰元素，环境概念图 + [Style Descriptor]`

## Required Config

必须提供：

- `YUNWU_API_TOKEN`

可选：

- `YUNWU_BASE_URL`（默认 `https://yunwu.ai`）
- `--model`（默认 `gemini-3.1-flash-image-preview`）
- `--image-size`（默认 `1K`）
- `--identity-map-json`（角色/道具基础图映射，可选）
- `--concurrency`（并发生成线程数，默认 `1` 串行）
- `--resolution-rule`（默认 `long-edge`，更适配 16:9 和 9:16）

默认模型与画质：

- 模型：`gemini-3.1-flash-image-preview`
- 尺寸档位：`1K`（可切 `2K`/`4K`）
- 风格预设：`photoreal-hq`（默认真实风格，超清质感）

默认画幅：

- 角色/道具/场景（assets）：`16:9`
- 分镜图（storyboard）：`9:16`

可通过参数覆盖：

- `--asset-aspect-ratio`
- `--storyboard-aspect-ratio`
- `--style`
- `--style-extra`

常用风格预设：

- `photoreal-hq`：真实风格，超清质感（默认）
- `cinematic`：电影感
- `anime`：动漫风格
- `cyberpunk`：赛博朋克
- `guofeng`：国风
- `fantasy-epic`：奇幻史诗
- `minimal-clean`：极简商业

## API Notes

默认按 Gemini 原生路由工作：

- `POST /v1beta/models/gemini-3.1-flash-image-preview:generateContent`

参考文档摘要见 [api-summary.md](./references/api-summary.md)。

## Script

主脚本： [run_banana_pipeline.py](./scripts/run_banana_pipeline.py)

示例：

```bash
export YUNWU_API_TOKEN="your-token"
export YUNWU_BASE_URL="https://yunwu.ai"

# 1) 只生成资产图（推荐先做这一步人工确认）
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --identity-map-json ./identity-map.json \
  --concurrency 4 \
  --style photoreal-hq \
  --model gemini-3.1-flash-image-preview \
  --image-size 1K \
  --output-dir ./outputs

# 2) 资产确认后生成分镜图
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase storyboard \
  --assets-json ./outputs/assets.generated.json \
  --model gemini-3.1-flash-image-preview \
  --image-size 1K \
  --output-dir ./outputs
```

## Output Files

- `assets.generated.json`
- `storyboard.generated.json`

输出 JSON 中包含每张图的 `image_path`、宽高和是否通过 1k 质量门。
