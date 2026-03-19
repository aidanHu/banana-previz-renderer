---
name: banana-previz-renderer
description: Generate production images with Banana Pro style image APIs using the structured JSON from gemini-video-story-adapter. Use when the user already has analysis output (asset_library and storyboard_script) and wants a pure image-generation pipeline: generate角色/道具/场景图 first, review them, then generate分镜图 with asset references and 1k resolution quality gate.
---

# Banana Previz Renderer

这个 skill 是“纯生图执行层”，不做剧情分析。

输入应来自上一个 skill（`gemini-video-story-adapter`）生成的结构化 JSON，至少包含：

- `asset_library`
- `storyboard_script`

## Workflow

1. 读取分析结果 JSON。
2. 先生成资产图：角色、道具、场景。
3. 进行验收（默认质量门：宽和高都不小于 1024）。
4. 资产确认后，再生成分镜图。

角色基图参考（如 Rumi/Jinu/Mira）：

- 不需要先转 URL。
- 直接提供映射 JSON（可混用本地路径和 URL，示例见 [identity-map.example.json](./assets/identity-map.example.json)）。
- 脚本会在对应资产生成请求里自动附加这些参考图。

引用链路（Gemini 原生）：

- 资产阶段会把返回的内联图片保存为本地文件（`image_path`）。
- 分镜阶段读取这些 `image_path`，转为 `inline_data` 作为参考图输入。
- 这个流程不依赖公网 URL。

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
