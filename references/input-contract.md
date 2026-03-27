# Input Contract

本 skill 读取上一个 skill 的输出 JSON 或 analysis 目录，期望至少有：

- `asset_library`: 数组
- `storyboard_script`: 数组

如果传入的是 analysis 目录，脚本会自动读取：

- `assets.json`
- `storyboard.json`

可选顶层字段：

- `style_descriptor`：会被拼接到所有最终 prompt

资产字段最低要求：

- `asset_tag`
- `asset_category`
- `full_prompt_string`

分镜字段最低要求：

- `shot_id`
- `first_frame_prompt`（优先）
- 或 `scela_prompt`（兜底）

可选但推荐：

- `referenced_assets`（用于显式声明分镜引用的资产；同时也会校验其是否在 `asset_library` 中定义）

Prompt 约定：

- 分镜中如果使用 `@实体` 标记，必须能映射到 `asset_library.asset_tag`
- 支持直接写完整 tag，例如 `@角色A`
- 也支持与 `asset_tag` 一致的简写内部引用，例如 `@场景A`、`@道具A`
