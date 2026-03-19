# Input Contract

本 skill 读取上一个 skill 的输出 JSON，期望至少有：

- `asset_library`: 数组
- `storyboard_script`: 数组

资产字段最低要求：

- `asset_tag`
- `asset_category`
- `full_prompt_string`

分镜字段最低要求：

- `shot_id`
- `first_frame_prompt`（优先）
- 或 `scela_prompt`（兜底）

可选但推荐：

- `referenced_assets`（用于把已生成资产图 URL 带进分镜请求）

