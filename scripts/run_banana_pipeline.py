#!/usr/bin/env python3
import argparse
import atexit
import base64
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://yunwu.ai"
DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
# HTTP 状态码分类：可重试 vs 不可重试
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# Pipeline 级心跳间隔（秒）
HEARTBEAT_INTERVAL = 10
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


# ---------------------------------------------------------------------------
# Lock File 防重入机制
# ---------------------------------------------------------------------------

_lock_path: str | None = None


def _resolve_lock_path(output_dir: str) -> Path:
    """在输出目录下创建 lock 文件。"""
    return Path(output_dir) / ".run_banana_pipeline.lock"


def acquire_lock(output_dir: str) -> None:
    """尝试获取 lock，如果已有同名活跃进程则拒绝启动。"""
    global _lock_path
    lock = _resolve_lock_path(output_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            old_pid = int(lock.read_text().strip())
            os.kill(old_pid, 0)
            print(
                f"error: 另一个 run_banana_pipeline 实例 (PID {old_pid}) 正在运行中。"
                f"如需强制重新运行，请先删除 {lock}",
                file=sys.stderr,
            )
            sys.exit(2)
        except (ProcessLookupError, ValueError):
            pass
    lock.write_text(str(os.getpid()))
    _lock_path = str(lock)
    atexit.register(_release_lock)


def _release_lock() -> None:
    """进程退出时自动清理 lock 文件。"""
    if _lock_path:
        try:
            Path(_lock_path).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pipeline 级心跳
# ---------------------------------------------------------------------------


class PipelineHeartbeat:
    """在 pipeline 执行期间每隔固定时间输出进度信息。"""

    def __init__(self, phase: str):
        self._phase = phase
        self._start = time.time()
        self._stop = threading.Event()
        self._completed = 0
        self._total = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self, total: int) -> None:
        self._total = total
        self._thread.start()

    def tick(self) -> None:
        """每完成一个 job 时调用。"""
        self._completed += 1

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(HEARTBEAT_INTERVAL)
            if not self._stop.is_set():
                elapsed = int(time.time() - self._start)
                print(
                    f"[heartbeat] {self._phase}: {self._completed}/{self._total} 完成 (已耗时 {elapsed}s)",
                    file=sys.stderr,
                    flush=True,
                )


# ---------------------------------------------------------------------------
# 不可重试异常（4xx 客户端错误专用）
# ---------------------------------------------------------------------------


class NonRetryableAPIError(Exception):
    """HTTP 4xx 等不可重试的 API 错误，_run_jobs_with_retry 不应重试此类错误。"""
    pass


class RetryableAPIError(Exception):
    """HTTP 429/5xx 等明确可重试的 API 错误。"""
    pass


class NonRetryableJobError(Exception):
    """本地后处理或响应内容问题，不应再次发起同一生图请求。"""
    pass


class UnknownJobError(Exception):
    """请求状态不明确，不能自动重试，避免重复提交。"""
    pass


def public_status_from_internal(internal_status: str) -> tuple[str, bool]:
    if internal_status == "ok":
        return "ok", True
    return "failed", False


def finalize_result(res: dict, internal_status: str, error: str = "") -> dict:
    status, success = public_status_from_internal(internal_status)
    res["status"] = status
    res["success"] = success
    res["failure_reason"] = error if not success else ""
    res["_internal_retry_state"] = internal_status
    if error:
        res["error"] = error
    else:
        res["error"] = ""
    return res


def stored_internal_status(item: dict) -> str:
    state = str(item.get("_internal_retry_state", "")).strip()
    if state:
        return state
    status = str(item.get("status", "")).strip()
    if status == "ok":
        return "ok"
    return "failed_retryable"


def parse_args() -> argparse.Namespace:
    env_base = os.getenv("YUNWU_BASE_URL") or DEFAULT_BASE_URL
    parser = argparse.ArgumentParser(
        description="Pure image rendering pipeline from gemini-video-story-adapter JSON."
    )
    parser.add_argument("--analysis-json", required=True, help="Input analysis JSON path.")
    parser.add_argument(
        "--identity-map-json",
        help="JSON path: map asset tag to reference images (local paths or URLs). Defaults to the shared skill identity-map if omitted.",
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
        default=3,
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
        default=600,
        help="Per-request timeout seconds. Timed-out jobs are marked failed and pipeline continues.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="最大重试次数，仅重试失败的图片。默认 2 次。",
    )
    parser.add_argument(
        "--asset-id",
        action="append",
        default=[],
        help="Target specific asset IDs to generate. Repeatable, e.g. --asset-id @角色_Rumi",
    )
    parser.add_argument(
        "--character",
        action="append",
        default=[],
        help="Target specific character role names to regenerate, e.g. --character Rumi",
    )
    parser.add_argument(
        "--shot-id",
        action="append",
        default=[],
        help="Target specific storyboard shot IDs to generate. Repeatable.",
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Ignore existing successful results and regenerate all jobs in the selected phase.",
    )
    return parser.parse_args()


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str, payload: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_identity_map_candidates() -> list[Path]:
    skill_root = Path(__file__).resolve().parent.parent
    env_path = os.getenv("BANANA_IDENTITY_MAP_JSON", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.home() / ".codex" / "skills" / "banana-previz-renderer" / "assets" / "identity-map.json")
    candidates.append(skill_root / "assets" / "identity-map.json")
    return candidates


def resolve_identity_map_path(path: str | None) -> Path | None:
    if path:
        candidate = Path(path).expanduser()
        return candidate if candidate.exists() else None
    for candidate in default_identity_map_candidates():
        if candidate.exists():
            return candidate
    return None


def load_existing_generated_items(path: Path, list_key: str, id_key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = payload.get(list_key)
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get(id_key, "")).strip()
        if item_id:
            out[item_id] = item
    return out


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


def sanitize_filename_component(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unnamed"
    text = text.replace("@", "")
    text = re.sub(r"[\\\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._") or "unnamed"


def image_basename(index: int, item_id: str) -> str:
    return f"{index:03d}_{sanitize_filename_component(item_id)}"


def next_available_image_path(out_dir: Path, name_prefix: str, ext: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename_component(name_prefix)
    candidate = out_dir / f"{base}{ext}"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = out_dir / f"{base}__v{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def sort_generated_items(items: list[dict], id_key: str) -> list[dict]:
    def sort_key(item: dict) -> tuple[str, str]:
        image_name = Path(str(item.get("image_path", "") or "")).name
        return (image_name, str(item.get(id_key, "")))

    return sorted(items, key=sort_key)


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
    """发送生图请求。仅对明确可重试的 HTTP 429/5xx 返回 retryable。"""
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
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
        if status not in RETRYABLE_STATUS_CODES:
            # 4xx 等不可重试错误，立即终止该 job 的所有重试
            print(
                f"[error] API 返回 HTTP {status}，属于不可重试错误。",
                file=sys.stderr, flush=True,
            )
            raise NonRetryableAPIError(
                f"HTTP {status}: {body[:200]}"
            ) from exc
        # 可重试错误：429/5xx
        wait_hint = ""
        if status == 429:
            retry_after = exc.headers.get("Retry-After", "")
            if retry_after:
                wait_hint = f" (Retry-After: {retry_after}s)"
        print(
            f"[warn] API 返回 HTTP {status}，可重试。{wait_hint}",
            file=sys.stderr, flush=True,
        )
        raise RetryableAPIError(f"HTTP {status}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise UnknownJobError(
            f"Network error after request submission state became ambiguous: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise UnknownJobError(
            "Request timed out and the provider may still be processing it."
        ) from exc


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
        raise NonRetryableJobError("API returned no inline image")
    first = image_parts[0]
    ext = guess_ext(first.get("mime_type", "image/png"))
    path = next_available_image_path(out_dir, name_prefix, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(first["data"]))
    width, height = read_image_dimensions(path)
    if width is None or height is None:
        raise NonRetryableJobError(f"Unable to read saved image dimensions: {path}")
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


def normalize_selector_values(values: list[str]) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        for part in re.split(r"[,，\s]+", str(value)):
            token = part.strip()
            if token:
                normalized.add(token.casefold())
    return normalized


def shot_selector_matches(shot_id: str, selectors: set[str]) -> bool:
    if not selectors:
        return False
    shot_norm = shot_id.casefold()
    if shot_norm in selectors:
        return True
    m = re.search(r"(\d+)$", shot_id)
    if not m:
        return False
    suffix = m.group(1)
    trimmed = suffix.lstrip("0") or "0"
    candidates = {suffix.casefold(), trimmed.casefold(), f"shot_{suffix}".casefold(), f"shot_{trimmed}".casefold()}
    return any(candidate in selectors for candidate in candidates)


def filter_asset_jobs(jobs: list[dict], args: argparse.Namespace) -> tuple[list[dict], set[str]]:
    selected_asset_ids = normalize_selector_values(args.asset_id)
    selected_characters = normalize_selector_values(args.character)
    if not selected_asset_ids and not selected_characters:
        return jobs, set()

    explicit_ids: set[str] = set()
    filtered: list[dict] = []
    for job in jobs:
        asset_id = str(job.get("id", "")).strip()
        asset_id_norm = asset_id.casefold()
        role_name = role_name_from_asset_tag(asset_id).casefold()
        if asset_id_norm in selected_asset_ids or (role_name and role_name in selected_characters):
            filtered.append(job)
            explicit_ids.add(asset_id)
    return filtered, explicit_ids


def load_identity_map(path: str | None) -> dict[str, list[str]]:
    resolved = resolve_identity_map_path(path)
    if not resolved:
        return {}
    raw = load_json(str(resolved))
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


def filter_storyboard_jobs(jobs: list[dict], args: argparse.Namespace) -> tuple[list[dict], set[str]]:
    selected_shot_ids = normalize_selector_values(args.shot_id)
    if not selected_shot_ids:
        return jobs, set()

    explicit_ids: set[str] = set()
    filtered: list[dict] = []
    for job in jobs:
        shot_id = str(job.get("shot_id", "")).strip()
        if shot_selector_matches(shot_id, selected_shot_ids):
            filtered.append(job)
            explicit_ids.add(shot_id)
    return filtered, explicit_ids


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


def _run_jobs_with_retry(
    jobs: list[tuple[int, dict]],
    worker_fn,
    args: argparse.Namespace,
    id_key: str,
    heartbeat: PipelineHeartbeat | None = None,
    existing_results: dict[str, dict] | None = None,
    persist_fn=None,
) -> list[dict]:
    """通用重试调度器：仅重试明确 retryable 的 job。"""
    results: dict[str, dict] = dict(existing_results or {})
    pending = list(jobs)
    max_retries = getattr(args, "max_retries", 2)

    for attempt in range(1 + max_retries):
        if not pending:
            break
        if attempt > 0:
            print(
                f"  ↻ 第 {attempt} 次重试，共 {len(pending)} 个失败项...",
                file=sys.stderr, flush=True,
            )

        round_results = []
        if args.concurrency <= 1:
            for idx, job in pending:
                round_results.append(worker_fn(idx, job))
                if heartbeat:
                    heartbeat.tick()
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futures = {ex.submit(worker_fn, idx, job): (idx, job) for idx, job in pending}
                for fut in concurrent.futures.as_completed(futures):
                    round_results.append(fut.result())
                    if heartbeat:
                        heartbeat.tick()

        # 记录本轮结果，成功的覆盖之前失败的
        next_pending = []
        for res in round_results:
            item_id = res.get(id_key, "")
            status = res.get("status")
            if status == "failed_retryable":
                res["retry_count"] = attempt
                results[item_id] = res
                if persist_fn:
                    persist_fn(results)
                # 找到原始 (idx, job) 以便下轮重试
                for idx, job in pending:
                    orig_id = job.get("id") or job.get("shot_id") or ""
                    if orig_id == item_id:
                        next_pending.append((idx, job))
                        break
            else:
                res["retry_count"] = attempt
                results[item_id] = res
                if persist_fn:
                    persist_fn(results)
        pending = next_pending

    # 按 id_key 排序返回
    return sort_generated_items(list(results.values()), id_key)


def run_assets_phase(args: argparse.Namespace, analysis: dict, token: str, output_dir: Path) -> dict:
    jobs, explicit_target_ids = filter_asset_jobs(build_asset_jobs(analysis), args)
    if (args.asset_id or args.character) and not jobs:
        raise ValueError("No asset jobs matched --asset-id/--character selectors.")
    identity_map = load_identity_map(args.identity_map_json)
    style_suffix = build_style_suffix(args)
    assets_path = output_dir / "assets.generated.json"
    existing_results = load_existing_generated_items(assets_path, "generated_assets", "id")
    done_ids = set()
    if not args.force_rerun:
        done_ids = {
            item_id
            for item_id, item in existing_results.items()
            if stored_internal_status(item) in {"ok", "failed_non_retryable", "unknown"}
        }
        done_ids -= explicit_target_ids
    skipped = len(done_ids)
    # 简化输出目录：去掉 images/ 中间层
    image_dir = output_dir / "assets"
    payload_base = {
        "phase": "assets",
        "provider": "gemini-generateContent",
        "model": args.model,
        "style": args.style,
        "style_description": style_suffix,
        "default_aspect_ratio": args.asset_aspect_ratio,
        "image_size": args.image_size,
        "min_resolution": args.min_resolution,
        "resolution_rule": args.resolution_rule,
    }

    def persist(results_map: dict[str, dict]) -> None:
        write_json(
            str(assets_path),
            {
                **payload_base,
                "resume_mode": "force_rerun" if args.force_rerun else "failed_only",
                "target_asset_ids": sorted(explicit_target_ids),
                "resumed_from_existing": bool(existing_results),
                "skipped_existing_ok": skipped,
                "generated_assets": sort_generated_items(list(results_map.values()), "id"),
            },
        )

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
            return finalize_result({
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
            }, "ok")
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
            image_path, width, height = save_first_image(images, image_dir, image_basename(idx, job["id"]))
            return finalize_result({
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
            }, "ok")
        except (NonRetryableAPIError, NonRetryableJobError) as exc:
            return finalize_result({
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
            }, "failed_non_retryable", str(exc))
        except RetryableAPIError as exc:
            return finalize_result({
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
            }, "failed_retryable", str(exc))
        except UnknownJobError as exc:
            return finalize_result({
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
            }, "unknown", str(exc))
        except Exception as exc:
            return finalize_result({
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
            }, "unknown", str(exc))

    indexed = [(idx, job) for idx, job in enumerate(jobs, start=1) if job["id"] not in done_ids]
    if existing_results:
        print(
            f"[info] assets phase 恢复已有结果：模式={'force_rerun' if args.force_rerun else 'failed_only'}，"
            f"跳过 {skipped} 个已成功项，待处理 {len(indexed)} 个。",
            file=sys.stderr,
            flush=True,
        )
    elif explicit_target_ids:
        print(
            f"[info] assets phase 定向生成 {len(explicit_target_ids)} 个目标。",
            file=sys.stderr,
            flush=True,
        )
    hb = PipelineHeartbeat("assets")
    hb.start(len(indexed))
    try:
        generated = _run_jobs_with_retry(
            indexed,
            worker,
            args,
            id_key="id",
            heartbeat=hb,
            existing_results=existing_results,
            persist_fn=persist,
        )
    finally:
        hb.stop()
    return {
        **payload_base,
        "resume_mode": "force_rerun" if args.force_rerun else "failed_only",
        "target_asset_ids": sorted(explicit_target_ids),
        "resumed_from_existing": bool(existing_results),
        "skipped_existing_ok": skipped,
        "generated_assets": sort_generated_items(generated, "id"),
    }


def run_storyboard_phase(
    args: argparse.Namespace, analysis: dict, assets_generated: dict, token: str, output_dir: Path
) -> dict:
    jobs, explicit_target_ids = filter_storyboard_jobs(build_storyboard_jobs(analysis, assets_generated), args)
    if args.shot_id and not jobs:
        raise ValueError("No storyboard jobs matched --shot-id selectors.")
    style_suffix = build_style_suffix(args)
    storyboard_path = output_dir / "storyboard.generated.json"
    existing_results = load_existing_generated_items(storyboard_path, "generated_storyboard", "shot_id")
    done_ids = set()
    if not args.force_rerun:
        done_ids = {
            item_id
            for item_id, item in existing_results.items()
            if stored_internal_status(item) in {"ok", "failed_non_retryable", "unknown"}
        }
        done_ids -= explicit_target_ids
    skipped = len(done_ids)
    # 简化输出目录：去掉 images/ 中间层
    image_dir = output_dir / "storyboard"
    payload_base = {
        "phase": "storyboard",
        "provider": "gemini-generateContent",
        "model": args.model,
        "style": args.style,
        "style_description": style_suffix,
        "default_aspect_ratio": args.storyboard_aspect_ratio,
        "image_size": args.image_size,
        "min_resolution": args.min_resolution,
        "resolution_rule": args.resolution_rule,
    }

    def persist(results_map: dict[str, dict]) -> None:
        write_json(
            str(storyboard_path),
            {
                **payload_base,
                "resume_mode": "force_rerun" if args.force_rerun else "failed_only",
                "target_shot_ids": sorted(explicit_target_ids),
                "resumed_from_existing": bool(existing_results),
                "skipped_existing_ok": skipped,
                "generated_storyboard": sort_generated_items(list(results_map.values()), "shot_id"),
            },
        )

    def worker(idx: int, job: dict) -> dict:
        prompt = (
            f"{job['prompt']}\n\n"
            f"画面比例：{args.storyboard_aspect_ratio}。\n"
            f"风格要求：{style_suffix}"
        )
        if args.dry_run:
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }, "ok")
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
            image_path, width, height = save_first_image(images, image_dir, image_basename(idx, job["shot_id"]))
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": image_path,
                "width": width,
                "height": height,
                "resolution_ok": resolution_pass(width, height, args.min_resolution, args.resolution_rule),
            }, "ok")
        except (NonRetryableAPIError, NonRetryableJobError) as exc:
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }, "failed_non_retryable", str(exc))
        except RetryableAPIError as exc:
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }, "failed_retryable", str(exc))
        except UnknownJobError as exc:
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }, "unknown", str(exc))
        except Exception as exc:
            return finalize_result({
                "shot_id": job["shot_id"],
                "prompt": prompt,
                "aspect_ratio": args.storyboard_aspect_ratio,
                "image_size": args.image_size,
                "reference_inputs": job["reference_inputs"],
                "image_path": "",
                "width": None,
                "height": None,
                "resolution_ok": None,
            }, "unknown", str(exc))

    indexed = [(idx, job) for idx, job in enumerate(jobs, start=1) if job["shot_id"] not in done_ids]
    if existing_results:
        print(
            f"[info] storyboard phase 恢复已有结果：模式={'force_rerun' if args.force_rerun else 'failed_only'}，"
            f"跳过 {skipped} 个已成功项，待处理 {len(indexed)} 个。",
            file=sys.stderr,
            flush=True,
        )
    elif explicit_target_ids:
        print(
            f"[info] storyboard phase 定向生成 {len(explicit_target_ids)} 个镜头。",
            file=sys.stderr,
            flush=True,
        )
    hb = PipelineHeartbeat("storyboard")
    hb.start(len(indexed))
    try:
        generated = _run_jobs_with_retry(
            indexed,
            worker,
            args,
            id_key="shot_id",
            heartbeat=hb,
            existing_results=existing_results,
            persist_fn=persist,
        )
    finally:
        hb.stop()
    return {
        **payload_base,
        "resume_mode": "force_rerun" if args.force_rerun else "failed_only",
        "target_shot_ids": sorted(explicit_target_ids),
        "resumed_from_existing": bool(existing_results),
        "skipped_existing_ok": skipped,
        "generated_storyboard": sort_generated_items(generated, "shot_id"),
    }


def main() -> int:
    try:
        args = parse_args()
        analysis = load_json(args.analysis_json)
        token = "" if args.dry_run else auth_token(args)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 防重入：dry-run 模式不需要 lock
        if not args.dry_run:
            acquire_lock(args.output_dir)

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
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
