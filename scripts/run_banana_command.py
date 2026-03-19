#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ASSET_TAG_RE = re.compile(r"@[^\s,，。；;]+")
SHOT_ID_RE = re.compile(r"\bshot[_ -]?(\d+)\b", re.IGNORECASE)
NUMBER_BLOCK_RE = re.compile(r"((?:\d+\s*[,，、/]\s*)*\d+)\s*号?(?:镜头|分镜)")
SINGLE_SHOT_RE = re.compile(r"(\d+)\s*号?(?:镜头|分镜)")
FORCE_WORDS = ("全部", "全量", "所有", "整体", "全部重生", "全部重生成", "全部重画", "全量重跑")
ASSET_WORDS = ("角色", "资产", "道具", "场景")
SHOT_WORDS = ("镜头", "分镜")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Natural-language wrapper for banana previz regenerate commands."
    )
    parser.add_argument("command", help="Natural-language command, e.g. 重生 Rumi 和 3、7 号镜头")
    parser.add_argument("--analysis-json", required=True, help="Input analysis JSON path.")
    parser.add_argument("--output-dir", default="./outputs", help="Output directory.")
    parser.add_argument("--assets-json", help="Existing assets.generated.json path for storyboard phase.")
    parser.add_argument("--identity-map-json", help="Identity map JSON path.")
    parser.add_argument("--base-url", help="API base URL override.")
    parser.add_argument("--model", help="Gemini image model override.")
    parser.add_argument("--token", help="API token override.")
    parser.add_argument("--style", help="Style preset override.")
    parser.add_argument("--style-extra", help="Extra style text.")
    parser.add_argument("--image-size", choices=["1K", "2K", "4K"], help="Image size override.")
    parser.add_argument("--concurrency", type=int, help="Concurrency override.")
    parser.add_argument("--request-timeout", type=int, help="Request timeout seconds.")
    parser.add_argument("--max-retries", type=int, help="Retry count override.")
    parser.add_argument("--asset-aspect-ratio", help="Asset aspect ratio override.")
    parser.add_argument("--storyboard-aspect-ratio", help="Storyboard aspect ratio override.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved pipeline command only.")
    return parser.parse_args()


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def split_numbers(block: str) -> list[str]:
    out = []
    for part in re.split(r"[,，、/\s]+", block):
        token = part.strip()
        if token.isdigit():
            out.append(token)
    return out


def normalize_shot_id(value: str) -> str:
    digits = str(int(value))
    return f"shot_{digits.zfill(3)}"


def parse_command(command: str, analysis: dict) -> dict:
    text = command.strip()
    text_casefold = text.casefold()

    asset_tags: set[str] = set(ASSET_TAG_RE.findall(text))
    characters: set[str] = set()
    shots: set[str] = set()

    known_roles: set[str] = set()
    for item in analysis.get("asset_library", []):
        if not isinstance(item, dict):
            continue
        tag = str(item.get("asset_tag", "")).strip()
        if tag.startswith("@角色_"):
            role_name = tag[len("@角色_") :].split("_", 1)[0].strip()
            if role_name:
                known_roles.add(role_name)
        if tag and tag in text:
            asset_tags.add(tag)

    for role_name in known_roles:
        if role_name.casefold() in text_casefold:
            characters.add(role_name)

    for match in SHOT_ID_RE.finditer(text):
        shots.add(normalize_shot_id(match.group(1)))
    for match in NUMBER_BLOCK_RE.finditer(text):
        for number in split_numbers(match.group(1)):
            shots.add(normalize_shot_id(number))
    for match in SINGLE_SHOT_RE.finditer(text):
        shots.add(normalize_shot_id(match.group(1)))

    explicit_force = any(word in text for word in FORCE_WORDS)
    mentions_assets = any(word in text for word in ASSET_WORDS) or bool(characters or asset_tags)
    mentions_shots = any(word in text for word in SHOT_WORDS) or bool(shots)

    if shots and not (characters or asset_tags):
        phase = "storyboard"
    elif (characters or asset_tags) and not shots:
        phase = "assets"
    elif shots and (characters or asset_tags):
        phase = "all"
    elif explicit_force and mentions_shots and not mentions_assets:
        phase = "storyboard"
    elif explicit_force and mentions_assets and not mentions_shots:
        phase = "assets"
    elif explicit_force:
        phase = "all"
    else:
        raise ValueError("Could not infer targets from command. Mention角色/资产/镜头/分镜 or explicit names/IDs.")

    return {
        "phase": phase,
        "asset_tags": sorted(asset_tags),
        "characters": sorted(characters),
        "shot_ids": sorted(shots),
        "force_rerun": explicit_force and not (asset_tags or characters or shots),
    }


def build_pipeline_command(args: argparse.Namespace, parsed: dict) -> list[str]:
    script_path = Path(__file__).with_name("run_banana_pipeline.py")
    cmd = [sys.executable, str(script_path), "--analysis-json", args.analysis_json, "--phase", parsed["phase"], "--output-dir", args.output_dir]

    assets_json = args.assets_json or str(Path(args.output_dir) / "assets.generated.json")
    if parsed["phase"] == "storyboard":
        cmd.extend(["--assets-json", assets_json])

    if parsed["phase"] == "all" and args.assets_json:
        cmd.extend(["--assets-json", args.assets_json])

    if args.identity_map_json:
        cmd.extend(["--identity-map-json", args.identity_map_json])
    if args.base_url:
        cmd.extend(["--base-url", args.base_url])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.token:
        cmd.extend(["--token", args.token])
    if args.style:
        cmd.extend(["--style", args.style])
    if args.style_extra:
        cmd.extend(["--style-extra", args.style_extra])
    if args.image_size:
        cmd.extend(["--image-size", args.image_size])
    if args.concurrency is not None:
        cmd.extend(["--concurrency", str(args.concurrency)])
    if args.request_timeout is not None:
        cmd.extend(["--request-timeout", str(args.request_timeout)])
    if args.max_retries is not None:
        cmd.extend(["--max-retries", str(args.max_retries)])
    if args.asset_aspect_ratio:
        cmd.extend(["--asset-aspect-ratio", args.asset_aspect_ratio])
    if args.storyboard_aspect_ratio:
        cmd.extend(["--storyboard-aspect-ratio", args.storyboard_aspect_ratio])

    for asset_id in parsed["asset_tags"]:
        cmd.extend(["--asset-id", asset_id])
    for character in parsed["characters"]:
        cmd.extend(["--character", character])
    for shot_id in parsed["shot_ids"]:
        cmd.extend(["--shot-id", shot_id])

    if parsed["force_rerun"]:
        cmd.append("--force-rerun")

    return cmd


def main() -> int:
    try:
        args = parse_args()
        analysis = load_json(args.analysis_json)
        parsed = parse_command(args.command, analysis)
        cmd = build_pipeline_command(args, parsed)

        print("[info] resolved command:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)

        if args.dry_run:
            return 0

        completed = subprocess.run(cmd)
        return int(completed.returncode)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
