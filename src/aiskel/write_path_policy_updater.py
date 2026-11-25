"""Module: @role: 新規作成ディレクトリに対する書き込みポリシー(ホワイトリスト/ブラックリスト)更新と通知を担当する。"""
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from .write_path_policy import WritePathMode, WritePathPolicy, strip_matching_quotes
from .write_path_policy_loader import (
    ALLOWED_WRITE_PATHS_KEYS,
    ALLOWED_WRITE_PATH_TXT_FILES,
    _collect_meta_dirs,
    _parse_write_policy_from_dict,
    _parse_write_policy_from_text,
    iter_meta_config_files,
    tomllib,
)

_NESTED_POLICY_CONTAINERS = ("write_paths", "write_policy")
_NESTED_ALLOW_KEYS = ("allow", "allowed", "whitelist", "allowed_write_paths")


def _resolve_under_root(path: Path, resolved_root: Path) -> Path:
    return path.resolve() if path.is_absolute() else (resolved_root / path).resolve()


def _is_within_project(path: Path, project_root: Path) -> bool:
    resolved_root = project_root.resolve()
    try:
        _resolve_under_root(path, resolved_root).relative_to(resolved_root)
        return True
    except ValueError:
        return False


def _is_under_dir(target_path: Path, dir_path: Path) -> bool:
    resolved_target = target_path.resolve()
    resolved_dir = dir_path.resolve()
    return resolved_target == resolved_dir or resolved_dir in resolved_target.parents


def find_new_top_directories(paths: Iterable[Path], project_root: Path) -> List[Path]:
    """書き込み・移動先パス群から、まだディスク上に存在しない最上位の新規ディレクトリ一覧を返す。"""
    resolved_root = project_root.resolve()
    top_dirs: List[Path] = []
    seen: Set[Path] = set()

    for raw_path in sorted(paths, key=lambda p: str(p)):
        parent = _resolve_under_root(raw_path, resolved_root).parent
        if parent == resolved_root or parent.exists():
            continue
        if not _is_within_project(parent, resolved_root):
            continue

        rel_parts = parent.relative_to(resolved_root).parts
        cur = resolved_root
        top_missing: Optional[Path] = None
        for part in rel_parts:
            cur = cur / part
            if not cur.exists():
                top_missing = cur
                break
        if top_missing is not None and top_missing not in seen:
            seen.add(top_missing)
            top_dirs.append(top_missing)

    return top_dirs


def _format_dir_pattern(dir_path: Path, project_root: Path) -> Optional[str]:
    resolved_root = project_root.resolve()
    try:
        rel = _resolve_under_root(dir_path, resolved_root).relative_to(resolved_root).as_posix()
    except ValueError:
        return None
    cleaned = rel.strip("/")
    if not cleaned or cleaned == ".":
        return None
    return f"{cleaned}/"


def _normalize_pattern_key(pattern: str) -> str:
    return strip_matching_quotes(pattern).replace("\\", "/").strip().rstrip("/")


def _append_patterns_to_json_file(target: Path, new_patterns: Sequence[str]) -> Tuple[bool, List[str]]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return False, []
    if not isinstance(data, dict):
        return False, []

    added: List[str] = []

    def _merge_into_list(target_list: List[object]) -> None:
        existing_keys = {
            _normalize_pattern_key(str(item))
            for item in target_list
            if isinstance(item, str) and item.strip()
        }
        for pat in new_patterns:
            if _normalize_pattern_key(pat) not in existing_keys:
                target_list.append(pat)
                existing_keys.add(_normalize_pattern_key(pat))
                added.append(pat)

    for container_key in _NESTED_POLICY_CONTAINERS:
        nested = data.get(container_key)
        if isinstance(nested, dict):
            for ak in _NESTED_ALLOW_KEYS:
                if ak in nested and isinstance(nested[ak], list):
                    _merge_into_list(nested[ak])
                    if added:
                        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    return True, added

    for ak in ALLOWED_WRITE_PATHS_KEYS:
        if ak in data:
            val = data[ak]
            if isinstance(val, list):
                _merge_into_list(val)
                if added:
                    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                return True, added
            if isinstance(val, str):
                lines = val.splitlines()
                existing_keys = {_normalize_pattern_key(l) for l in lines if l.strip()}
                for pat in new_patterns:
                    if _normalize_pattern_key(pat) not in existing_keys:
                        lines.append(pat)
                        existing_keys.add(_normalize_pattern_key(pat))
                        added.append(pat)
                if added:
                    data[ak] = "\n".join(lines)
                    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                return True, added

    return False, []


def _append_patterns_to_txt_file(target: Path, new_patterns: Sequence[str]) -> Tuple[bool, List[str]]:
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return False, []
    existing_keys = {
        _normalize_pattern_key(line)
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith(("#", "//", ";"))
    }
    added = [pat for pat in new_patterns if _normalize_pattern_key(pat) not in existing_keys]
    if not added:
        return True, []
    sep = "" if (not content or content.endswith("\n")) else "\n"
    target.write_text(content + sep + "\n".join(added) + "\n", encoding="utf-8")
    return True, added


def _find_closing_bracket_pos(lines: List[str], start_idx: int, open_col: int) -> Tuple[int, int]:
    # なぜ必要か: クォート内のGlob文字クラス([...])やコメント内の]で配列終端を誤認しないため
    quote_char: Optional[str] = None
    depth = 0
    for row in range(start_idx, len(lines)):
        line = lines[row]
        col = open_col if row == start_idx else 0
        while col < len(line):
            ch = line[col]
            if quote_char:
                if ch == quote_char:
                    quote_char = None
            else:
                if ch in ('"', "'"):
                    quote_char = ch
                elif ch == "#":
                    break
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return row, col
            col += 1
    return -1, -1


def _append_patterns_to_text_config(target: Path, new_patterns: Sequence[str]) -> Tuple[bool, List[str]]:
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return False, []

    _, _, existing_allows, _ = _parse_write_policy_from_text(content)
    existing_keys = {_normalize_pattern_key(p) for p in existing_allows}
    to_add = [pat for pat in new_patterns if _normalize_pattern_key(pat) not in existing_keys]
    if not to_add:
        return True, []

    candidate_keys = tuple(dict.fromkeys(ALLOWED_WRITE_PATHS_KEYS + _NESTED_ALLOW_KEYS))
    keys_alt = "|".join(re.escape(k) for k in candidate_keys)
    key_re = re.compile(rf"^([ \t]*)({keys_alt})[ \t]*([:=])[ \t]*(.*)$")
    lines = content.splitlines()

    for idx, line in enumerate(lines):
        m = key_re.match(line)
        if not m:
            continue
        base_indent = m.group(1)
        key_name = m.group(2)
        rhs = m.group(4).strip()

        if key_name in _NESTED_ALLOW_KEYS and len(base_indent) == 0:
            continue

        if rhs.startswith("["):
            open_col = line.find("[", len(base_indent))
            bracket_end, close_col = _find_closing_bracket_pos(lines, idx, open_col)
            if bracket_end == -1:
                return False, []
            if bracket_end == idx:
                before = lines[idx][:close_col].rstrip()
                after = lines[idx][close_col:]
                formatted = ", ".join(f'"{p}"' for p in to_add)
                if before.endswith("["):
                    lines[idx] = f"{before}{formatted}{after}"
                elif before.endswith(","):
                    lines[idx] = f"{before} {formatted}{after}"
                else:
                    lines[idx] = f"{before}, {formatted}{after}"
            else:
                item_indent = base_indent + "  "
                for k in range(idx + 1, bracket_end):
                    stripped_k = lines[k].strip()
                    if stripped_k and not stripped_k.startswith("#"):
                        item_indent = lines[k][: len(lines[k]) - len(lines[k].lstrip(" \t"))]
                        break
                prev_idx = bracket_end - 1
                while prev_idx > idx and (not lines[prev_idx].strip() or lines[prev_idx].strip().startswith("#")):
                    prev_idx -= 1
                if prev_idx > idx and not lines[prev_idx].rstrip().endswith((",", "[")):
                    lines[prev_idx] = lines[prev_idx].rstrip() + ","
                insert_lines = [f'{item_indent}"{p}",' for p in to_add]
                lines = lines[:bracket_end] + insert_lines + lines[bracket_end:]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True, to_add

        if not rhs or rhs.startswith("#"):
            last_item_idx = idx
            item_indent = base_indent + "  "
            is_nested_mapping = False
            j = idx + 1
            base_indent_len = len(base_indent.expandtabs(2))
            while j < len(lines):
                stripped_next = lines[j].strip()
                if not stripped_next or stripped_next.startswith("#"):
                    j += 1
                    continue
                next_indent = len(lines[j][: len(lines[j]) - len(lines[j].lstrip(" \t"))].expandtabs(2))
                if next_indent < base_indent_len:
                    break
                if stripped_next.startswith("-"):
                    item_indent = lines[j][: len(lines[j]) - len(lines[j].lstrip(" \t"))]
                    last_item_idx = j
                    j += 1
                    continue
                if last_item_idx == idx and next_indent > base_indent_len:
                    is_nested_mapping = True
                break
            if is_nested_mapping:
                continue
            insert_lines = [f'{item_indent}- "{p}"' for p in to_add]
            lines = lines[: last_item_idx + 1] + insert_lines + lines[last_item_idx + 1 :]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True, to_add

    return False, []


def _inspect_config_whitelist(target: Path) -> Tuple[bool, bool]:
    """(非空allowリスト保持, 空または定義済みallowキー保持) を返す。"""
    try:
        content = target.read_text(encoding="utf-8").strip()
    except OSError:
        return False, False
    if not content:
        return False, False

    if target.suffix.lower() == ".json" or content.startswith("{"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                found, _, allows, _ = _parse_write_policy_from_dict(data)
                if found:
                    has_key = any(k in data for k in ALLOWED_WRITE_PATHS_KEYS) or any(
                        isinstance(data.get(c), dict) and any(ak in data[c] for ak in _NESTED_ALLOW_KEYS)
                        for c in _NESTED_POLICY_CONTAINERS
                    )
                    return bool(allows), bool(allows or has_key)
            return False, False
        except Exception:
            if target.suffix.lower() == ".json":
                return False, False

    if target.suffix.lower() == ".toml" and tomllib is not None:
        try:
            data = tomllib.loads(content)
            if isinstance(data, dict):
                found, _, allows, _ = _parse_write_policy_from_dict(data)
                if found:
                    has_key = any(k in data for k in ALLOWED_WRITE_PATHS_KEYS) or any(
                        isinstance(data.get(c), dict) and any(ak in data[c] for ak in _NESTED_ALLOW_KEYS)
                        for c in _NESTED_POLICY_CONTAINERS
                    )
                    return bool(allows), bool(allows or has_key)
            return False, False
        except Exception:
            return False, False

    found, _, allows, _ = _parse_write_policy_from_text(content)
    if found:
        has_key = any(
            re.search(rf"^[ \t]*{re.escape(k)}[ \t]*[:=]", content, re.MULTILINE)
            for k in ALLOWED_WRITE_PATHS_KEYS
        )
        return bool(allows), bool(allows or has_key)
    return False, False


def _find_active_whitelist_source_file(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Optional[Path]:
    # なぜ必要か: config.jsonの空配列とallowed_write_paths.txt併用時にtxt側の既存許可リストが上書き無効化されるのを防ぐため
    resolved_root = project_root.resolve()
    fallback_config: Optional[Path] = None

    for target in iter_meta_config_files(resolved_root, output_dir):
        has_non_empty, has_key = _inspect_config_whitelist(target)
        if has_non_empty:
            return target
        if has_key and fallback_config is None:
            fallback_config = target

    for d in _collect_meta_dirs(resolved_root, output_dir):
        for fname in ALLOWED_WRITE_PATH_TXT_FILES:
            txt_file = d / fname
            if txt_file.is_file():
                return txt_file

    return fallback_config


def update_write_policy_for_created_dirs(
    project_root: Path,
    created_dirs: Sequence[Path],
    policy: WritePathPolicy,
    output_dir: Optional[Path] = None,
    dry_run: bool = False,
    created_files: Optional[Sequence[Path]] = None,
    persist: bool = True,
) -> Tuple[List[Path], Optional[Path]]:
    """新規作成されたディレクトリをホワイトリストへ反映し、適用中ポリシーに応じた通知を行う。"""
    if not created_dirs or not policy.is_active or policy.mode == WritePathMode.DISABLED:
        return [], None

    resolved_root = project_root.resolve()
    resolved_files = [_resolve_under_root(f, resolved_root) for f in (created_files or ())]
    dir_entries: List[Tuple[Path, str]] = []
    for d in created_dirs:
        pat = _format_dir_pattern(d, resolved_root)
        if pat:
            dir_entries.append((d, pat))
    if not dir_entries:
        return [], None

    patterns = [pat for _, pat in dir_entries]
    added_dirs: List[Path] = []
    updated_file: Optional[Path] = None

    if policy.has_whitelist_rule:
        existing_keys = {_normalize_pattern_key(p) for p in policy.allow_patterns}
        pending_entries: List[Tuple[Path, str]] = []
        for d, pat in dir_entries:
            if _normalize_pattern_key(pat) in existing_keys:
                continue
            files_under_d = [f for f in resolved_files if _is_under_dir(f, d)]
            already_covered = (
                all(policy._matches_any(f, policy.allow_patterns) for f in files_under_d)
                if files_under_d
                else policy._matches_any(d, policy.allow_patterns)
            )
            if not already_covered:
                pending_entries.append((d, pat))

        pending_patterns = [pat for _, pat in pending_entries]

        if dry_run:
            if pending_patterns:
                print(f"ℹ [DRY-RUN] 新規作成ディレクトリがホワイトリスト追加対象になります: {', '.join(pending_patterns)}")
            else:
                print(f"ℹ [DRY-RUN] ホワイトリスト適用中: 新規ディレクトリ作成予定: {', '.join(patterns)}")
            return [d for d, _ in pending_entries], None

        source_file = _find_active_whitelist_source_file(resolved_root, output_dir) if persist else None
        if source_file is not None and pending_patterns:
            ok = False
            added_pats: List[str] = []
            if source_file.suffix.lower() == ".json":
                ok, added_pats = _append_patterns_to_json_file(source_file, pending_patterns)
            elif source_file.suffix.lower() == ".txt":
                ok, added_pats = _append_patterns_to_txt_file(source_file, pending_patterns)
            else:
                ok, added_pats = _append_patterns_to_text_config(source_file, pending_patterns)

            if ok and added_pats:
                updated_file = source_file
                added_set = {_normalize_pattern_key(p) for p in added_pats}
                added_dirs = [d for d, pat in pending_entries if _normalize_pattern_key(pat) in added_set]
                try:
                    disp_cfg = source_file.relative_to(resolved_root)
                except ValueError:
                    disp_cfg = source_file
                print(f"✨ ホワイトリスト(allowed_write_paths)に新規作成ディレクトリを追加しました ({disp_cfg}): {', '.join(added_pats)}")
            elif not ok:
                print(
                    f"⚠ ホワイトリスト設定ファイルへの自動追記に対応していない形式のため、以下を手動追加してください ({source_file}): {', '.join(pending_patterns)}",
                    file=sys.stderr,
                )
        elif pending_patterns:
            print(f"ℹ ホワイトリスト適用中: 新規ディレクトリを作成しました: {', '.join(pending_patterns)}")
        else:
            print(f"ℹ ホワイトリスト適用中: 新規ディレクトリを作成しました (許可リストに含まれています): {', '.join(patterns)}")

    # AUTOモード等でブラックリスト併用時に誤って禁止リストへ追加し書き込み不能になるのを防ぐため通知のみ行う
    if policy.has_blacklist_rule:
        prefix = "[DRY-RUN] " if dry_run else ""
        print(f"ℹ {prefix}ブラックリスト適用中: 新規ディレクトリを作成しました (禁止リスト対象外): {', '.join(patterns)}")

    return added_dirs, updated_file
