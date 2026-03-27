# banana-previz-renderer

用于带前置 guardrails 的“纯生图执行” skill。  
输入上一个 skill 的结构化 JSON，批量生成角色/道具/场景图，以及后续分镜图。

## 当前默认

- 默认模型：`gemini-3.1-flash-image-preview`
- 默认尺寸：`1K`
- 默认比例：资产 `16:9`，分镜 `9:16`
- 默认风格：`photoreal-hq`

## 当前 guardrails

- 预检查 `storyboard_script` 中的 `@实体` 和 `referenced_assets`，未定义资产直接报错
- 分镜请求前把 `@实体` 替换为对应资产的 `full_prompt_string`
- 自动替换高风险内容词，并强制追加安全后缀
- 命中儿童关键词的资产会触发儿童安全护栏
- 全部请求都会追加统一光影质量基底

## 目录结构

- `scripts/run_banana_pipeline.py`：主执行脚本
- `assets/character-refs/*`：历史内置角色参考图资源（不再作为默认输入）
- `assets/identity-map.json`：历史示例映射（不再默认自动加载）
- `assets/identity-map.example.json`：用户参考图映射示例
- `references/api-summary.md`：API 摘要
- `references/input-contract.md`：输入契约说明

## 环境变量

- 必填：`YUNWU_API_TOKEN`
- 可选：`YUNWU_BASE_URL`（默认 `https://yunwu.ai`）
- 可选：`BANANA_IDENTITY_MAP_JSON`（为当前任务提供用户自定义参考图映射 JSON）

## 角色参考图映射

- 推荐显式传 `--identity-map-json /path/to/role-refs.json`
- 如果未传 `--identity-map-json`，脚本不会默认加载内置角色参考图
- `BANANA_IDENTITY_MAP_JSON` 仅作为当前任务的可选映射来源，不再代表共享默认角色库
- identity-map 里的本地图片路径可以使用绝对路径；如使用相对路径，则相对 identity-map 文件自身解析
- 推荐映射格式示例：

```json
{
  "@角色A": ["/absolute/path/to/role-a.jpg"],
  "@角色B": ["/absolute/path/to/role-b.jpg"]
}
```

- 若某角色未提供参考图，脚本仍会生成该角色资产，只是不注入参考图

## 常用命令

仅生成资产图（推荐先跑）：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis \
  --phase assets \
  --identity-map-json ./role-refs.json \
  --output-dir ./outputs \
  --style photoreal-hq \
  --image-size 1K
```

仅生成分镜图：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis \
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

## Prompt 处理规则

- assets phase 会在资产原始描述后追加结构化出图约束、光影基底、风格描述和安全后缀
- storyboard phase 会先展开 `@角色A` / `@道具A` / `@场景A` 这类内部资产 tag，再注入 guardrails
- 如果分析 JSON 顶层包含 `style_descriptor`，会和 `--style` / `--style-extra` 一起拼接进最终 prompt

## 定向重生成

也支持自然语言入口脚本：`scripts/run_banana_command.py`

自然语言示例：

```bash
python3 ./scripts/run_banana_command.py \
  --analysis-json ./analysis.json \
  --identity-map-json ./role-refs.json \
  --output-dir ./outputs \
  "重生 角色A 和 3、7 号镜头"
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
  --identity-map-json ./role-refs.json \
  --output-dir ./outputs \
  --character 角色A,角色B
```

只重生成指定资产：

```bash
python3 ./scripts/run_banana_pipeline.py \
  --analysis-json ./analysis.json \
  --phase assets \
  --identity-map-json ./role-refs.json \
  --output-dir ./outputs \
  --asset-id @角色A,@场景A
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

- 角色参考图完全来自用户提供的 identity-map JSON
- 只要映射里存在对应 `asset_tag`（如 `@角色A`），该角色就会注入参考图
- 未在映射中声明的角色不会自动上传任何参考图
- 如需更高保真，可在当前任务的 identity-map 中提供你自己的本地原图
- 默认不再自动使用 skill 内置角色参考图

## 输出文件

- `assets.generated.json`
- `storyboard.generated.json`
- `assets/*`
- `storyboard/*`

图片文件名规则：

- 资产图：`001_角色A.png`、`009_场景A.png`
- 分镜图：`001_shot_001.png`
- 若同名文件已存在，会自动追加 `__v2`、`__v3`，不覆盖旧文件
- `assets.generated.json` / `storyboard.generated.json` 也会按最终文件名排序

输出 JSON 额外会记录：

- `guardrails`
- `child_safety_guardrail`
- `referenced_assets`（storyboard）

## 备注

更完整说明见同目录 `SKILL.md`。
