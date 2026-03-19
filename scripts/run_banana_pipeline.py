#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
import mimetypes
import os
import struct
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://yunwu.ai"
DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
STYLE_PRESETS = {
    "photoreal-hq": "默认真实风格，超清质感，电影级写实光影，细节清晰，质地真实。",
    "cinematic": "电影感风格，戏剧化布光，镜头语言明确，高对比细节，叙事氛围强。",
    "anime": "高质量动漫风格，干净线条，层次分明，色彩鲜明，角色表现力强。",
    "cyberpunk": "赛博朋克风格，霓虹光效，夜景氛围，高反差，高密度科技细节。",
    "guofeng": "国风美术风格，东方审美，雅致配色，传统元素与现代构图结合。",
    "fantasy-epic": "奇幻史诗风格，宏大场景，丰富材质，强烈空间层次与氛围感。",
    "minimal-clean": "极简商业风格，画面干净，主体突出，背景克制，信息表达清晰。",
}
REFERENCE_ROLE_NAMES = [
    "Rumi",
    "Mira",
    "Zoey",
    "Jinu",
    "Abby",
    "Baby saja",
    "Mystery",
    "Romance",
]


def parse_args() -> argparse.Namespace:
    env_base = os.getenv("YUNWU_BASE_URL") or DEFAULT_BASE_URL
    parser = argparse.ArgumentParser(
        description="Pure image rendering pipeline from gemini-video-story-adapter JSON."
    )
    parser.add_argument("--analysis-json", required=True, help="Input analysis JSON path.")
    parser.add_argument(
        "--identity-map-json",
        help="JSON path: map asset tag to reference images (local paths or URLs).",
    )
    parser.add_argument(
        "--phase",
        choices=["assets", "storyboard", "all"],
        default="assets",
        help="assets: generate assets; storyboard: generate storyboard; all: run both.",
    )
    parser.add_argument(
        "--assets-json",
        help="Existing assets.generated.json path (required for phase=storyboard unless phase=all).",
    )
    parser.add_argument("--output-dir", default="./outputs", help="Output directory.")
    parser.add_argument("--base-url", default=env_base, help="API base URL.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini image model.")
    parser.add_argument("--token", help="API token; defaults to YUNWU_API_TOKEN.")
    parser.add_argument("--num-images", type=int, default=1, help="Number of images per request.")
    parser.add_argument(
        "--style",
        choices=list(STYLE_PRESETS.keys()),
        default="photoreal-hq",
        help="Built-in visual style preset. Default is photoreal-hq.",
    )
    parser.add_argument(
        "--style-extra",
        default="",
        help="Optional extra style sentence appended after the preset.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Parallel workers for image generation. 1 means serial.",
    )
    parser.add_argument("--asset-aspect-ratio", default="16:9", help="Aspect ratio for assets.")
    parser.add_argument("--storyboard-aspect-ratio", default="9:16", help="Aspect ratio for storyboard.")
    parser.add_argument(
        "--image-size",
        choices=["1K", "2K", "4K"],
        default="1K",
        help="Gemini image size. Default 1K.",
    )
    parser.add_argument("--min-resolution", type=int, default=1024, help="Min width/height quality gate.")
    parser.add_argument(
        "--resolution-rule",
        choices=["long-edge", "short-edge", "both-sides"],
        default="long-edge",
        help="Resolution validation rule. Default long-edge is recommended for 16:9 and 9:16.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build jobs only; do not call API.")
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=90,
        help="Per-request timeout seconds. Timed-out jobs are marked failed and pipeline continues.",
    )
    return parser.parse_args()


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str, payload: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def auth_token(args: argparse.Namespace) -> str:
    token = args.token or os.getenv("YUNWU_API_TOKEN")
    if not token:
        raise ValueError("Missing token. Set --token or YUNWU_API_TOKEN.")
    return token


def is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"}


def guess_ext(mime_type: str) -> str:
    ext = mimetypes.guess_extension(mime_type) or ""
    if ext == ".jpe":
        return ".jpg"
    return ext or ".png"


def read_image_dimensions(path: Path) -> tuple[int | None, int | None]:
    data = path.read_bytes()
    is_png = len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = len(data) >= 4 and data[:2] == b"\xff\xd8"
    if is_png:
        width, height = struct.unpack(">LL", data[16:24])
        return int(width), int(height)
    if is_jpeg:
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC9, 0xCA, 0xCB):
                h = struct.unpack(">H", data[i + 5 : i + 7])[0]
                w = struct.unpack(">H", data[i + 7 : i + 9])[0]
                return int(w), int(h)
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg_len
    return None, None


def resolution_pass(
    width: int | None, height: int | None, min_resolution: int, rule: str
) -> bool | None:
    if width is None or height is None:
        return None
    long_edge = max(width, height)
    short_edge = min(width, height)
    if rule == "long-edge":
        return long_edge >= min_resolution
    if rule == "short-edge":
        return short_edge >= min_resolution
    return width >= min_resolution and height >= min_resolution


def post_gemini_generate(
    base_url: str,
    model: str,
    token: str,
    prompt: str,
    aspect_ratio: str,
    image_size: str,
    num_images: int,
    request_timeout: int,
    reference_inputs: list[str] | None = None,
) -> dict:
    parts = []
    for ref in reference_inputs or []:
        if is_url(ref):
            mime_type = mimetypes.guess_type(ref)[0] or "image/jpeg"
            parts.append(
                {
                    "file_data": {
                        "mime_type": mime_type,
                        "file_uri": ref,
                    }
                }
            )
        else:
            ref_path = Path(ref)
            mime_type = mimetypes.guess_type(str(ref_path))[0] or "image/png"
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(ref_path.read_bytes()).decode("ascii"),
                    }
                }
            )
    parts.append({"text": prompt})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "candidateCount": num_images,
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size,
            },
        },
    }

    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent?key="
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=request_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_inline_images(response: dict) -> list[dict]:
    out = []
    for cand in response.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            if inline.get("data"):
                out.append(
                    {
                        "mime_type": inline.get("mimeType") or inline.get("mime_type") or "image/png",
                        "data": inline["data"],
                    }
                )
    return out


def save_first_image(image_parts: list[dict], out_dir: Path, name_prefix: str) -> tuple[str, int | None, int | None]:
    if not image_parts:
        return "", None, None
    first = image_parts[0]
    ext = guess_ext(first.get("mime_type", "image/png"))
    path = out_dir / f"{name_prefix}{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(first["data"]))
    width, height = read_image_dimensions(path)
    return str(path), width, height


def build_asset_jobs(analysis: dict) -> list[dict]:
    jobs = []
    for item in analysis.get("asset_library", []):
        asset_tag = item.get("asset_tag")
        prompt = item.get("full_prompt_string") or item.get("visual_anchor")
        if not asset_tag or not prompt:
            continue
        jobs.append(
            {
                "id": asset_tag,
                "type": item.get("asset_category", "unknown"),
                "prompt": prompt,
                "layout": item.get("layout", ""),
            }
        )
    return jobs


def load_identity_map(path: str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    raw = load_json(path)
    mapped: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            mapped[key] = [value]
        elif isinstance(value, list):
            mapped[key] = [v for v in value if isinstance(v, str)]
    return mapped


def role_name_from_asset_tag(asset_tag: str) -> str:
    prefix = "@角色_"
    if not asset_tag.startswith(prefix):
        return ""
    raw = asset_tag[len(prefix) :]
    if not raw:
        return ""
    # @角色_Rumi_冰 -> Rumi
    return raw.split("_", 1)[0].strip()


def filter_reference_inputs(asset_id: str, asset_type: str, identity_map: dict[str, list[str]]) -> list[str]:
    refs = identity_map.get(asset_id, [])
    if not refs:
        return []
    t = str(asset_type or "")
    # Role references are only allowed for explicitly named canonical roles.
    if "角色" in t or t.lower() == "character":
        role_name = role_name_from_asset_tag(asset_id)
        if role_name not in REFERENCE_ROLE_NAMES:
            return []
    return refs


def build_storyboard_jobs(analysis: dict, assets_generated: dict) -> list[dict]:
    asset_ref_map = {}
    for item in assets_generated.get("generated_assets", []):
        aid = item.get("id")
        refs = []
        if item.get("image_path"):
            refs.append(item["image_path"])
        if item.get("image_url"):
            refs.append(item["image_url"])
        if aid and refs:
            asset_ref_map[aid] = refs
    jobs = []
    for shot in analysis.get("storyboard_script", []):
        shot_id = shot.get("shot_id")
        prompt = shot.get("first_frame_prompt") or shot.get("scela_prompt")
        if not shot_id or not prompt:
            continue
        refs = []
        for aid in shot.get("referenced_assets", []):
            if aid in asset_ref_map:
                refs.extend(asset_ref_map[aid])
        jobs.append({"shot_id": shot_id, "prompt": prompt, "reference_inputs": refs})
    return jobs


def build_style_suffix(args: argparse.Namespace) -> str:
    style_text = STYLE_PRESETS.get(args.style, STYLE_PRESETS["photoreal-hq"])
    if args.style_extra.strip():
        return f"{style_text} {args.style_extra.strip()}"
    return style_text


def build_asset_type_constraints(asset_type: str) -> str:
    t = str(asset_type or "")
    if "角色" in t:
        return "背景要求：纯白背景（#FFFFFF），仅保留单角色主体，不得出现任何场景元素、文字贴纸或其他实体。"
    if "道具" in t:
        return "背景要求：纯白背景（#FFFFFF），仅保留单道具主体，不得出现桌面、手部或任何场景元素。"
    if "场景" in t:
        return "场景要求：环境内容丰富，层次清晰，光线明亮，色彩鲜艳。"
    return ""


def run_assets_phase(args: argparse.Namespace, analysis: dict, token: str, output_dir: Path) -> dict:
    jobs = build_asset_jobs(analysis)
    identity_map = load_identity_map(args.identity_map_json)
    style_suffix = build_style_suffix(args)
    generated = []
    image_dir = output_dir / "images" / "assets"
    def worker(idx: int, job: dict) -> dict:
        type_constraints = build_asset_type_constraints(job.get("type", ""))
        prompt = (
            f"{job['prompt']}\n\n"
            f"{type_constraints}\n"
            f"画面比例：{args.asset_aspect_ratio}。\n"
            f"风格要求：{style_suffix}"
        )
        reference_paths = filter_reference_inputs(job["id"], str(job.get("type", "")), identity_map)
        if args.dry_run:
            return {
                "id": job["id"],
                "type": job["type"],
                "prompt": prompt,
                "layout": job["layout"],
                "aspect_ratio": args.asset_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": reference_paths,
                "image_path": "",
                "image_url": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }
        try:
            resp = post_gemini_generate(
                args.base_url,
                args.model,
                token,
                prompt,
                args.asset_aspect_ratio,
                args.image_size,
                args.num_images,
                args.request_timeout,
                reference_inputs=reference_paths,
            )
            images = extract_inline_images(resp)
            image_path, width, height = save_first_image(images, image_dir, f"asset_{idx:03d}")
            return {
                "id": job["id"],
                "type": job["type"],
                "prompt": prompt,
                "layout": job["layout"],
                "aspect_ratio": args.asset_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": reference_paths,
                "image_path": image_path,
                "image_url": "",
                "width": width,
                "height": height,
                "resolution_ok": resolution_pass(width, height, args.min_resolution, args.resolution_rule),
                "status": "ok",
            }
        except Exception as exc:
            return {
                "id": job["id"],
                "type": job["type"],
                "prompt": prompt,
                "layout": job["layout"],
                "aspect_ratio": args.asset_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": reference_paths,
                "image_path": "",
                "image_url": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
                "status": "failed",
                "error": str(exc),
            }

    indexed = list(enumerate(jobs, start=1))
    if args.concurrency <= 1:
        for idx, job in indexed:
            generated.append(worker(idx, job))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = [ex.submit(worker, idx, job) for idx, job in indexed]
            for fut in concurrent.futures.as_completed(futures):
                generated.append(fut.result())
        generated.sort(key=lambda x: x["id"])
    return {
        "phase": "assets",
        "provider": "gemini-generateContent",
        "model": args.model,
        "style": args.style,
        "style_description": style_suffix,
        "default_aspect_ratio": args.asset_aspect_ratio,
        "image_size": args.image_size,
        "min_resolution": args.min_resolution,
        "resolution_rule": args.resolution_rule,
        "generated_assets": generated,
    }


def run_storyboard_phase(
    args: argparse.Namespace, analysis: dict, assets_generated: dict, token: str, output_dir: Path
) -> dict:
    jobs = build_storyboard_jobs(analysis, assets_generated)
    style_suffix = build_style_suffix(args)
    generated = []
    image_dir = output_dir / "images" / "storyboard"
    def worker(idx: int, job: dict) -> dict:
        prompt = (
            f"{job['prompt']}\n\n"
            f"画面比例：{args.storyboard_aspect_ratio}。\n"
            f"风格要求：{style_suffix}"
        )
        if args.dry_run:
            return {
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }
        try:
            resp = post_gemini_generate(
                args.base_url,
                args.model,
                token,
                prompt,
                args.storyboard_aspect_ratio,
                args.image_size,
                args.num_images,
                args.request_timeout,
                reference_inputs=job["reference_inputs"],
            )
            images = extract_inline_images(resp)
            image_path, width, height = save_first_image(images, image_dir, f"shot_{idx:03d}")
            return {
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": image_path,
                "width": width,
                "height": height,
                "resolution_ok": resolution_pass(width, height, args.min_resolution, args.resolution_rule),
                "status": "ok",
            }
        except Exception as exc:
            return {
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
                "status": "failed",
                "error": str(exc),
            }

    indexed = list(enumerate(jobs, start=1))
    if args.concurrency <= 1:
        for idx, job in indexed:
            generated.append(worker(idx, job))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = [ex.submit(worker, idx, job) for idx, job in indexed]
            for fut in concurrent.futures.as_completed(futures):
                generated.append(fut.result())
        generated.sort(key=lambda x: x["shot_id"])
    return {
        "phase": "storyboard",
        "provider": "gemini-generateContent",
        "model": args.model,
        "style": args.style,
        "style_description": style_suffix,
        "default_aspect_ratio": args.storyboard_aspect_ratio,
        "image_size": args.image_size,
        "min_resolution": args.min_resolution,
        "resolution_rule": args.resolution_rule,
        "generated_storyboard": generated,
    }


def main() -> int:
    try:
        args = parse_args()
        analysis = load_json(args.analysis_json)
        token = "" if args.dry_run else auth_token(args)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        assets_path = output_dir / "assets.generated.json"
        storyboard_path = output_dir / "storyboard.generated.json"

        if args.phase in {"assets", "all"}:
            assets = run_assets_phase(args, analysis, token, output_dir)
            write_json(str(assets_path), assets)
        else:
            if not args.assets_json:
                raise ValueError("phase=storyboard requires --assets-json (or use --phase all).")
            assets = load_json(args.assets_json)

        if args.phase in {"storyboard", "all"}:
            storyboard = run_storyboard_phase(args, analysis, assets, token, output_dir)
            write_json(str(storyboard_path), storyboard)

        return 0
    except Exception as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
