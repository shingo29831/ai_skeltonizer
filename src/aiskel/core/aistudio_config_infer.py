"""Module: @role: 設定ファイル(ai_meta/config.json)推論モードの対象解決・プロンプト統合・パッチ検証を担当する。"""
import contextlib
import io
import json
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..write_path_policy import WritePathMode, normalize_write_path_mode
from .aistudio_handoff.text import format_summary_prompt_text, load_project_summary_text
from .aistudio_prompts import build_config_infer_prompt, load_default_prompt
from .patch_applier_content import (
    apply_empty_search_chunk,
    apply_new_file_chunk,
    apply_search_replace_chunk,
)
from .patch_applier_file_ops import ensure_file_buffer_loaded
from .patch_applier_types import PatchApplyState
from .patch_matcher import find_and_replace
from .patch_parser import has_patch_delimiters, parse_patch_chunks

DEFAULT_CONFIG_REL_PATH = "ai_meta/config.json"
ALT_CONFIG_REL_PATH = ".ai_meta/config.json"
_VALID_WRITE_PATH_MODES = frozenset(mode.value for mode in WritePathMode)

def resolve_target_config_file(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, str]:
    """設定推論対象となるプロジェクト内 config.json の絶対パスと相対POSIXパスを返す。"""
    resolved_root = project_root.resolve()
    if output_dir is not None:
        resolved_out = output_dir.resolve()
        alt = (resolved_out / "config.json").resolve()
        if alt.is_file() and alt.is_relative_to(resolved_root):
            return alt, alt.relative_to(resolved_root).as_posix()

    primary = (resolved_root / DEFAULT_CONFIG_REL_PATH).resolve()
    if primary.is_file():
        return primary, DEFAULT_CONFIG_REL_PATH

    hidden_cfg = (resolved_root / ALT_CONFIG_REL_PATH).resolve()
    if hidden_cfg.is_file():
        return hidden_cfg, ALT_CONFIG_REL_PATH

    if output_dir is not None:
        resolved_out = output_dir.resolve()
        alt = (resolved_out / "config.json").resolve()
        if alt.is_relative_to(resolved_root):
            return alt, alt.relative_to(resolved_root).as_posix()

    return primary, DEFAULT_CONFIG_REL_PATH

def load_project_summary_for_config_infer(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> str:
    """aiskel同期を実行して最新要約バンドルと ai_meta/config.json を準備し、要約文字列を返す。"""
    summary_text = load_project_summary_text(project_root)
    resolved_root = project_root.resolve()
    config_path, _ = resolve_target_config_file(resolved_root, output_dir)
    if not config_path.is_file():
        from ..config import ensure_default_config_file

        target_dir = (
            output_dir.resolve()
            if output_dir is not None and output_dir.resolve().is_relative_to(resolved_root)
            else config_path.parent
        )
        ensure_default_config_file(target_dir, resolved_root)
    return summary_text

def _read_config_file_text(config_path: Path) -> str:
    raw = config_path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

def _build_safe_code_fence(content: str) -> str:
    max_run = 0
    for match in re.finditer(r"`+", content):
        if len(match.group(0)) > max_run:
            max_run = len(match.group(0))
    return "`" * max(3, max_run + 1)

def _format_config_file_section(config_path: Path, rel_path: str) -> Tuple[str, bool]:
    if not config_path.is_file():
        return (
            f"=== 現在の設定ファイル ({rel_path}) ===\n"
            f"注意: 現在 `{rel_path}` は未存在のため、モードB（新規作成）で `{rel_path}` を作成してください。",
            False,
        )
    content = _read_config_file_text(config_path)
    fence = _build_safe_code_fence(content)
    body = content.rstrip("\r\n")
    code_block = f"{fence}json\n{body}\n{fence}" if body else f"{fence}json\n{fence}"
    return f"=== 現在の設定ファイル ({rel_path}) ===\nファイルパス: {rel_path}\n{code_block}", True

def build_config_infer_combined_text(
    project_root: Path,
    summary_text: str,
    output_dir: Optional[Path] = None,
) -> Tuple[str, str, bool]:
    """設定ファイル作成専用プロンプト・プロジェクト要約・ai_meta/config.json を統合して返す。"""
    config_path, rel_path = resolve_target_config_file(project_root, output_dir)
    config_section, config_exists = _format_config_file_section(config_path, rel_path)
    default_prompt = load_default_prompt(project_root)
    infer_prompt = build_config_infer_prompt(
        default_prompt=default_prompt,
        target_rel_path=rel_path,
        config_exists=config_exists,
        project_root=project_root,
    )
    prompt_and_summary = format_summary_prompt_text(infer_prompt, summary_text)
    combined = f"{prompt_and_summary}\n\n{config_section}" if prompt_and_summary else config_section
    return combined, rel_path, config_exists

def _validate_string_or_string_list(data: Dict[str, object], key: str) -> Optional[str]:
    if key not in data:
        return None
    val = data[key]
    if isinstance(val, str):
        return None
    if isinstance(val, list) and all(isinstance(item, str) for item in val):
        return None
    return f"`{key}` は文字列または文字列配列で指定してください"

def _validate_config_schema(data: object) -> Optional[str]:
    if not isinstance(data, dict):
        return "config.json のルート要素は JSON オブジェクト ({...}) である必要があります"

    if "bundle_filename" in data:
        val = data["bundle_filename"]
        if not isinstance(val, str) or not val.strip():
            return "`bundle_filename` は空でない文字列で指定してください"
        stripped_name = val.strip()
        if (
            "/" in stripped_name
            or "\\" in stripped_name
            or ":" in stripped_name
            or any(ord(ch) < 32 for ch in stripped_name)
            or stripped_name in (".", "..")
            or Path(stripped_name).name != stripped_name
        ):
            return "`bundle_filename` にパス区切り文字・制御文字・相対パス指定は使用できません"

    if "description" in data and not isinstance(data["description"], str):
        return "`description` は文字列で指定してください"

    for key in (
        "test_command",
        "static_analysis_command",
        "exclude_paths",
        "allowed_write_paths",
        "denied_write_paths",
    ):
        err = _validate_string_or_string_list(data, key)
        if err:
            return err

    if "write_paths_mode" in data:
        mode_val = data["write_paths_mode"]
        if not isinstance(mode_val, str) or not mode_val.strip():
            return "`write_paths_mode` は文字列で指定してください"
        # normalize_write_path_modeのautoフォールバックを通さず正規モード値のみ許可
        if mode_val.strip().lower() not in _VALID_WRITE_PATH_MODES:
            return "`write_paths_mode` は auto / whitelist / blacklist / both / disabled のいずれかで指定してください"
        normalized_mode = normalize_write_path_mode(mode_val)
        if normalized_mode in (WritePathMode.WHITELIST, WritePathMode.BOTH):
            raw_allows = data.get("allowed_write_paths")
            has_allows = bool(
                (isinstance(raw_allows, str) and raw_allows.strip())
                or (isinstance(raw_allows, list) and any(isinstance(x, str) and x.strip() for x in raw_allows))
            )
            if not has_allows:
                return f"`write_paths_mode` が `{mode_val}` の場合、`allowed_write_paths` を空にできません"

    if "patch_handoff_max_tabs" in data:
        val = data["patch_handoff_max_tabs"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            return "`patch_handoff_max_tabs` は 0 以上の整数で指定してください"

    for key in ("bloat_max_lines", "bloat_max_chars", "organize_max_files"):
        if key in data:
            val = data[key]
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                return f"`{key}` は 1 以上の整数で指定してください"

    return None

def validate_config_infer_patch(
    patch_text: str,
    project_root: Path,
    target_rel_path: str = DEFAULT_CONFIG_REL_PATH,
) -> Optional[Tuple[str, str]]:
    """設定推論モードのパッチが対象ファイル限定・適用可能・有効なJSONであることを検証する。"""
    resolved_root = project_root.resolve()
    resolved_target = (resolved_root / target_rel_path).resolve()

    if not has_patch_delimiters(patch_text):
        short_msg = "設定推論パッチに置換ブロック(<<<< ==== >>>>)がありません"
        return short_msg, (
            f"設定推論モードでは `{target_rel_path}` に対する置換ブロック(<<<< ==== >>>>)を含めて出力してください。"
        )

    chunks, parse_failed_details, parse_fail_count = parse_patch_chunks(patch_text, resolved_root)
    if parse_fail_count > 0 or not chunks:
        detail = "\n".join(parse_failed_details) if parse_failed_details else "適用可能なパッチブロックがありません"
        short_msg = "設定推論パッチの構文解析に失敗しました"
        return short_msg, f"設定推論パッチの構文に誤りがあります:\n{detail}"

    disallowed_targets = []
    forbidden_ops = []
    for chunk in chunks:
        if chunk.file_path is None:
            disallowed_targets.append(f"- 行 {chunk.block_line_no}: ファイルパス指定がありません")
            continue
        try:
            disp = chunk.file_path.relative_to(resolved_root).as_posix()
        except ValueError:
            disp = str(chunk.file_path)
        if chunk.file_path.resolve() != resolved_target:
            disallowed_targets.append(f"- 行 {chunk.block_line_no}: {disp}")
        elif chunk.is_file_move or chunk.is_file_deletion or chunk.is_standalone_file_delete or chunk.del_node_name:
            forbidden_ops.append(f"- 行 {chunk.block_line_no}: {disp} に対する移動・削除操作は不可")

    if disallowed_targets or forbidden_ops:
        lines = disallowed_targets + forbidden_ops
        short_msg = f"設定推論ポリシー違反: {target_rel_path} 以外の変更または削除・移動が含まれています"
        report = (
            f"設定推論モードでは `{target_rel_path}` の内容更新のみが許可されています。\n"
            "以下の箇所がポリシーに違反しています:\n"
            + "\n".join(lines)
            + f"\n\n`{target_rel_path}` のみを対象とした完全版パッチを再提示してください。"
        )
        return short_msg, report

    # 既存の write_paths_mode に関係なく仮想バッファで適用結果とJSON妥当性を検証
    state = PatchApplyState()
    buf_out = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_out):
        for chunk in chunks:
            current_file = chunk.file_path
            if current_file is None:
                continue
            ensure_file_buffer_loaded(state, current_file, is_file_deletion=False)
            content = state.file_contents[current_file]
            if content is None:
                return "設定ファイルの読み込みに失敗しました", f"`{target_rel_path}` がテキストとして読み込めません。"
            is_empty_search = not any(line.strip() for line in chunk.search_lines)
            if (current_file.exists() and current_file not in state.files_to_delete) or content:
                if is_empty_search:
                    apply_empty_search_chunk(
                        state,
                        current_file,
                        Path(target_rel_path),
                        resolved_root,
                        content,
                        chunk.replace_lines,
                    )
                else:
                    apply_search_replace_chunk(
                        state,
                        current_file,
                        Path(target_rel_path),
                        content,
                        chunk.search_lines,
                        chunk.replace_lines,
                        chunk.block_line_no,
                        find_and_replace_fn=find_and_replace,
                    )
            else:
                apply_new_file_chunk(state, current_file, Path(target_rel_path), chunk.replace_lines)

    if state.fail_count > 0 or (state.success_count == 0 and state.skipped_count == 0):
        detail = "\n".join(state.failed_details) if state.failed_details else "適用可能な変更がありません"
        short_msg = f"{target_rel_path} へのパッチ適用検証に失敗しました"
        report = (
            f"提示されたパッチを現行の `{target_rel_path}` に仮想適用したところ失敗しました:\n"
            f"{detail}\n\n現行の `{target_rel_path}` と一致する完全版パッチを再提示してください。"
        )
        return short_msg, report

    updated_text = state.file_contents.get(resolved_target)
    if updated_text is None or not updated_text.strip():
        return f"{target_rel_path} が空になります", f"適用後の `{target_rel_path}` が空になるため拒否しました。"

    try:
        parsed_json = json.loads(updated_text)
    except json.JSONDecodeError as je:
        short_msg = f"{target_rel_path} のJSON構文が不正です ({je.lineno}行目)"
        report = (
            f"パッチ適用後の `{target_rel_path}` に JSON 構文エラーがあります:\n"
            f"- {je.lineno}行 {je.colno}列: {je.msg}\n\n"
            "コメントや末尾カンマを含まない有効な JSON に修正した完全版パッチを再提示してください。"
        )
        return short_msg, report

    schema_err = _validate_config_schema(parsed_json)
    if schema_err:
        short_msg = f"{target_rel_path} の設定値が不正です"
        report = (
            f"パッチ適用後の `{target_rel_path}` の設定項目に不備があります:\n"
            f"- {schema_err}\n\n上記を修正した完全版パッチを再提示してください。"
        )
        return short_msg, report

    return None
