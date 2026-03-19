# API Summary (Gemini 3 Pro Image Preview via Yunwu)

参考页面：

- `https://yunwu.apifox.cn/api-379838953`

默认路由：

- `POST /v1beta/models/gemini-3.1-flash-image-preview:generateContent`

常见请求体（文生图）：

```json
{
  "contents": [
    {
      "role": "user",
      "parts": [
        { "text": "prompt" }
      ]
    }
  ],
  "generationConfig": {
    "responseModalities": ["IMAGE"],
    "candidateCount": 1,
    "imageConfig": {
      "aspectRatio": "16:9",
      "imageSize": "1K"
    }
  }
}
```

常见返回：

- 图片通常出现在 `candidates[].content.parts[].inlineData`（base64）。
- 本 skill 会把 base64 图片落地到本地文件 `image_path`。

本 skill 的策略：

1. 默认 `imageSize=1K`。
2. 资产默认画幅 `16:9`；分镜默认画幅 `9:16`。
3. 本地解析图片宽高并按 `min_resolution=1024` 进行质量门校验。
