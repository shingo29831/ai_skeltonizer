"""Module: @role: Google AI Studioの応答テキストから安全なaiskel cコマンドを抽出・検証・正規化する。"""
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .code_extractor import parse_target_spec

_PROMPT_PREFIX_RE = re.compile(r"^\s*(?:[$>]\s+)?")
_COMMAND_PREFIX_RE = re.compile(r"^(?:aiskel|skelify)\s+(?:c|copy|cp|-c|--copy)(?:\s+|$)", re.IGNORECASE)
_FORBIDDEN_SHELL_META_RE = re.compile(r"[;&|`$<>\r\n]")
_PATCH_START_RE = re.compile(r"^\s*<{4,}(?:\s.*)?$")
_PATCH_MID_RE = re.compile(r"^\s*={4,}(?:\s.*)?$")
_PATCH_END_RE = re.compile(r"^\s*>{4,}(?:\s.*)?$")
_ALLOWED_BOOL_FLAGS = {
    "-d",
    "--deps",
    "--dependencies",
    "--no-focus",
    "--no-paste",
    "--no-error-clipboard",
}


class CommandValidationError(ValueError):
    """許可されていないコマンド形式・オプション・パス指定を検出した際の例外。"""


def _find_valid_patch_line_indices(lines: List[str]) -> Set[int]:
    """<<<<〜====〜>>>> が完結するパッチブロック行のみを除外対象として特定する。"""
    excluded: Set[int] = set()
    total = len(lines)
    i = 0
    while i < total:
        if _PATCH_START_RE.match(lines[i]):
            has_mid = False
            end_idx = -1
            for j in range(i + 1, total):
                if _PATCH_START_RE.match(lines[j]):
                    break
                if _PATCH_MID_RE.match(lines[j]):
                    has_mid = True
                    continue
                if _PATCH_END_RE.match(lines[j]):
                    if has_mid:
                        end_idx = j
                    break
            if end_idx != -1:
                excluded.update(range(i, end_idx + 1))
                i = end_idx + 1
                continue
        i += 1
    return excluded


def _scan_command_tokens(command_line: str, strict: bool) -> List[str]:
    """Windowsバックスラッシュを保持しつつ部分クォートと行末コメントを処理してトークン化する。"""
    if "\r" in command_line or "\n" in command_line:
        if strict:
            raise CommandValidationError(f"複数行コマンドは許可されていません: {command_line}")
        return []

    tokens: List[str] = []
    buf: List[str] = []
    token_started = False
    in_quote: Optional[str] = None

    for ch in command_line:
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            else:
                buf.append(ch)
            continue

        if ch in ("'", '"'):
            in_quote = ch
            token_started = True
            continue

        if ch.isspace():
            if token_started:
                tokens.append("".join(buf))
                buf = []
                token_started = False
            continue

        # クォート外の空白直後の # 以降を行末コメントとして安全に除去
        if ch == "#" and not token_started:
            break

        buf.append(ch)
        token_started = True

    if in_quote is not None and strict:
        raise CommandValidationError(f"コマンドのクォートが閉じられていません: {command_line}")

    if token_started:
        tokens.append("".join(buf))

    if strict:
        for tok in tokens:
            if _FORBIDDEN_SHELL_META_RE.search(tok):
                raise CommandValidationError(f"危険なシェル記号を含むコマンドは実行できません: {command_line}")

    return tokens


def _tokenize_command_line(command_line: str) -> List[str]:
    return _scan_command_tokens(command_line, strict=True)


def _has_copy_target_argument(command_line: str) -> bool:
    """行末コメントやオプションを除いた上でコピー対象引数が1つ以上含まれるか判定する。"""
    if not _COMMAND_PREFIX_RE.match(command_line):
        return False

    tokens = _scan_command_tokens(command_line, strict=False)
    if len(tokens) < 3:
        return False
    if tokens[0].lower() not in ("aiskel", "skelify"):
        return False
    if tokens[1].lower() not in ("c", "copy", "cp", "-c", "--copy"):
        return False

    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if tok in _ALLOWED_BOOL_FLAGS or tok.startswith("--max-chars="):
            i += 1
            continue
        if tok == "--max-chars":
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        # プロンプト説明文中の省略記号・プレースホルダー引用(<path>:<func>等)による誤抽出を除外
        if tok in ("...", "…") or (tok.startswith("<") and ">" in tok):
            i += 1
            continue
        return True
    return False


def extract_aiskel_copy_commands(text: str) -> List[str]:
    """応答テキストまたはコードブロック文字列からパッチ本体とダミー行を除いたaiskel cコマンド行を抽出する。"""
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    # 閉じ記号のない単独<<<<で後続コマンドが全無視されないよう完結ブロックのみ除外
    patch_indices = _find_valid_patch_line_indices(lines)

    commands: List[str] = []
    seen: Set[str] = set()
    for idx, raw_line in enumerate(lines):
        if idx in patch_indices:
            continue
        cleaned = _PROMPT_PREFIX_RE.sub("", raw_line).strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        # オプション直後に#コメントのみ続くダミー行が他コマンドを巻き添え失敗させないよう除外
        if _has_copy_target_argument(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            commands.append(cleaned)
    return commands


def _parse_positive_int(raw_val: str, flag_name: str) -> int:
    try:
        parsed = int(raw_val)
    except ValueError as e:
        raise CommandValidationError(f"{flag_name} には正の整数を指定してください: {raw_val}") from e
    if parsed <= 0:
        raise CommandValidationError(f"{flag_name} には正の整数を指定してください: {raw_val}")
    return parsed


def _validate_spec_within_root(spec: str, project_root: Path) -> str:
    try:
        file_part, _ = parse_target_spec(spec)
    except ValueError as e:
        raise CommandValidationError(f"不正な対象指定です: {spec} ({e})") from e

    cleaned_file = file_part.strip()
    if not cleaned_file or cleaned_file in (".", "./", ".\\", "...", "…"):
        raise CommandValidationError(f"ファイルパスが空または無効です: {spec}")

    resolved_root = project_root.resolve()
    raw_path = Path(cleaned_file)
    candidate = raw_path.resolve() if raw_path.is_absolute() else (resolved_root / raw_path).resolve()

    if not candidate.is_relative_to(resolved_root) or candidate == resolved_root:
        raise CommandValidationError(f"プロジェクトルート外のパス指定は禁止されています: {spec}")
    return spec


def _parse_copy_command(
    command_line: str,
    project_root: Path,
) -> Tuple[List[str], bool, Optional[int], bool, bool, bool]:
    cleaned = _PROMPT_PREFIX_RE.sub("", command_line).strip()
    tokens = _tokenize_command_line(cleaned)
    if len(tokens) < 3:
        raise CommandValidationError(f"対象指定が不足しています: {command_line}")

    prog = tokens[0].lower()
    subcmd = tokens[1].lower()
    if prog not in ("aiskel", "skelify") or subcmd not in ("c", "copy", "cp", "-c", "--copy"):
        raise CommandValidationError(f"aiskel c 以外のコマンドは自動実行できません: {command_line}")

    specs: List[str] = []
    with_deps = False
    max_chars: Optional[int] = None
    cmd_no_focus = False
    cmd_no_paste = False
    cmd_no_error_clipboard = False

    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            raise CommandValidationError(f"空の対象指定は許可されていません: {command_line}")
        if tok in _ALLOWED_BOOL_FLAGS:
            if tok in ("-d", "--deps", "--dependencies"):
                with_deps = True
            elif tok == "--no-focus":
                cmd_no_focus = True
            elif tok == "--no-paste":
                cmd_no_paste = True
            elif tok == "--no-error-clipboard":
                cmd_no_error_clipboard = True
            i += 1
        elif tok == "--max-chars":
            if i + 1 >= len(tokens):
                raise CommandValidationError("--max-chars の値が指定されていません")
            max_chars = _parse_positive_int(tokens[i + 1], "--max-chars")
            i += 2
        elif tok.startswith("--max-chars="):
            raw_val = tok.split("=", 1)[1].strip()
            max_chars = _parse_positive_int(raw_val, "--max-chars")
            i += 1
        elif tok.startswith("-"):
            raise CommandValidationError(f"自動実行で許可されていないオプションです: {tok}")
        else:
            specs.append(_validate_spec_within_root(tok, project_root))
            i += 1

    if not specs:
        raise CommandValidationError(f"コピー対象ファイルが指定されていません: {command_line}")

    return specs, with_deps, max_chars, cmd_no_focus, cmd_no_paste, cmd_no_error_clipboard


def _format_display_token(token: str) -> str:
    if any(ch.isspace() for ch in token):
        return f'"{token}"'
    return token


def build_safe_copy_argv(
    commands: List[str],
    project_root: Path,
    no_focus: bool = False,
    no_paste: bool = False,
    no_error_clipboard: bool = False,
) -> Tuple[List[str], str]:
    """検証済みのaiskel cコマンド群を安全なCLI引数リストと表示用コマンド文字列へ統合する。"""
    if not commands:
        raise CommandValidationError("実行するコマンドが空です")

    merged_specs: List[str] = []
    seen_specs: Set[str] = set()
    merged_deps = False
    merged_max_chars: Optional[int] = None
    merged_no_focus = no_focus
    merged_no_paste = no_paste
    merged_no_error_clipboard = no_error_clipboard

    for cmd in commands:
        specs, with_deps, max_chars, cmd_nf, cmd_np, cmd_nec = _parse_copy_command(cmd, project_root)
        merged_deps = merged_deps or with_deps
        merged_no_focus = merged_no_focus or cmd_nf
        merged_no_paste = merged_no_paste or cmd_np
        merged_no_error_clipboard = merged_no_error_clipboard or cmd_nec
        if max_chars is not None:
            merged_max_chars = max_chars if merged_max_chars is None else min(merged_max_chars, max_chars)
        for spec in specs:
            if spec not in seen_specs:
                seen_specs.add(spec)
                merged_specs.append(spec)

    argv: List[str] = ["copy", "--dir", str(project_root.resolve())]
    display_parts: List[str] = ["aiskel", "c"]

    if merged_deps:
        argv.append("-d")
        display_parts.append("-d")
    if merged_max_chars is not None:
        argv.extend(["--max-chars", str(merged_max_chars)])
        display_parts.extend(["--max-chars", str(merged_max_chars)])
    if merged_no_focus:
        argv.append("--no-focus")
    if merged_no_paste:
        argv.append("--no-paste")
    if merged_no_error_clipboard:
        argv.append("--no-error-clipboard")

    argv.extend(merged_specs)
    display_parts.extend(_format_display_token(spec) for spec in merged_specs)
    return argv, " ".join(display_parts)
