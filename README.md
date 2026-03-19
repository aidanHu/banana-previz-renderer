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
- `assets/character-refs/*`：内置角色参考图
- `assets/identity-map.json`：repo 内默认角色映射
- `assets/identity-map.example.json`：角色参考图映射示例
- `references/api-summary.md`：API 摘要
- `references/input-contract.md`：输入契约说明

## 环境变量

- 必填：`YUNWU_API_TOKEN`
- 可选：`YUNWU_BASE_URL`（默认 `https://yunwu.ai`）
- 可选：`BANANA_IDENTITY_MAP_JSON`（覆盖默认共享映射路径）

## 共享 identity-map

- 默认共享路径：`~/.codex/skills/banana-previz-renderer/assets/identity-map.json`
- 不传 `--identity-map-json` 时，脚本会优先读取这个共享文件
- repo 内也保留一份 `.agents/skills/banana-previz-renderer/assets/identity-map.json`，方便本地开发和迁移
- 项目目录里的 `identity-map.json` 不再是默认约定；只有你显式传参时才会使用

## 常用命令

仅生成资产图（推荐先跑）：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
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

- 支持 `--request-timeout`（默认 `600` 秒）
- 单任务超时/失败不会中断全批次
- 用户侧最终只看两类：`status=ok` 或 `status=failed`
- 未成功项会带 `failure_reason`
- 系统内部只会自动重试“明确可重试失败”的任务
- 请求状态不明确的任务不会自动重试，避免重复提交
- 默认恢复模式是“只补明确可重试失败项”，不会重跑历史成功项和不明确项
- 如需定向重生成，可用 `--character` / `--asset-id` / `--shot-id`
- 如需全量重跑，显式传 `--force-rerun`

## 定向重生成

也支持自然语言入口脚本：`scripts/run_banana_command.py`

自然语言示例：

```bash
python3 ./scripts/run_banana_command.py \
  --analysis-json ./analysis.json \
  --output-dir ./outputs \
  "重生 Rumi 和 3、7 号镜头"
```

```bash
python3 ./scripts/run_banana_command.py \
  --analysis-json ./analysis.json \
  --output-dir ./outputs \
  "全量重跑所有分镜"
```

只重生成指定角色：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --output-dir ./outputs \
  --character Rumi,Jinu
```

只重生成指定资产：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --output-dir ./outputs \
  --asset-id @角色_Rumi,@场景_水族馆
```

只重生成指定分镜：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase storyboard \
  --assets-json ./outputs/assets.generated.json \
  --output-dir ./outputs \
  --shot-id shot_003,7
```

## 参考图策略

- 只对“命名映射表中的角色”注入参考图（Rumi/Mira/Zoey/Jinu/Abby/Baby saja/Mystery/Romance）
- 未命名角色（如 `@角色_Doctor`）不会自动上传参考图

## 输出文件

- `assets.generated.json`
- `storyboard.generated.json`
- `assets/*`
- `storyboard/*`

图片文件名规则：

- 资产图：`001_角色_Rumi.png`、`009_场景_水族馆.png`
- 分镜图：`001_shot_001.png`
- 若同名文件已存在，会自动追加 `__v2`、`__v3`，不覆盖旧文件
- `assets.generated.json` / `storyboard.generated.json` 也会按最终文件名排序

## 备注

更完整说明见同目录 `SKILL.md`。
