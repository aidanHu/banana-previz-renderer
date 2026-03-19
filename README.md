# banana-previz-renderer

用于“纯生图执行”的 skill。  
输入上一个 skill 的结构化 JSON，批量生成角色/道具/场景图，以及后续分镜图。

## 当前默认

- 默认模型：`gemini-3.1-flash-image-preview`
- 默认尺寸：`1K`
- 默认比例：资产 `16:9`，分镜 `9:16`
- 默认风格：`photoreal-hq`

## 目录结构

- `scripts/run_banana_pipeline.py`：主执行脚本
- `assets/identity-map.example.json`：角色参考图映射示例
- `references/api-summary.md`：API 摘要
- `references/input-contract.md`：输入契约说明

## 环境变量

- 必填：`YUNWU_API_TOKEN`
- 可选：`YUNWU_BASE_URL`（默认 `https://yunwu.ai`）

## 常用命令

仅生成资产图（推荐先跑）：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --identity-map-json ./identity-map.json \
  --output-dir ./outputs \
  --style photoreal-hq \
  --image-size 1K
```

仅生成分镜图：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase storyboard \
  --assets-json ./outputs/assets.generated.json \
  --output-dir ./outputs
```

## 失败处理（已支持）

- 支持 `--request-timeout`（默认 `90` 秒）
- 单任务超时/失败不会中断全批次
- 失败项会写入输出 JSON：`status: failed` + `error`

## 参考图策略

- 只对“命名映射表中的角色”注入参考图（Rumi/Mira/Zoey/Jinu/Abby/Baby saja/Mystery/Romance）
- 未命名角色（如 `@角色_Doctor`）不会自动上传参考图

## 输出文件

- `assets.generated.json`
- `storyboard.generated.json`
- `images/assets/*`
- `images/storyboard/*`

## 备注

更完整说明见同目录 `SKILL.md`。
