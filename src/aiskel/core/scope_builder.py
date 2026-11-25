"""Module: @role: 1度目探索用の軽量ディレクトリインデックス生成、および指定パス群＋直接依存先のスコープ限定バンドル生成を担当する。"""
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..parsers.base_parser import DependencyEntry, RoleEntry, generate_dependency_map_text, generate_role_map_text
from ..parsers.language_detector import get_parser_for_file
from .bundle_builder import _discover_policy_text
from .code_extractor import detect_language
from .scanner import _extract_dir_role, generate_tree_text, get_target_files

DEFAULT_INDEX_FILENAMES: Tuple[str, ...] = (
    "directory_index.txt",
    "directory_index.md",
    "directory_index.xml",
)
DEFAULT_EXTRA_TEXT_MAX_CHARS = 8000
_SCRIPT_EXT_CANDIDATES: Tuple[str, ...] = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".dart", ".java", ".cs", ".swift", ".cpp", ".hpp", ".c", ".h",
)

@dataclass
class ScopeResolutionResult:
    """スコープ解決結果の対象ファイル群・直接依存ファイル群・警告メッセージを保持する。"""

    primary_files: List[Path]
    dependency_files: List[Path]
    all_scoped_files: List[Path]
    missing_specs: List[str]
    resolved_specs: List[str]

def _read_text_safely(file_path: Path) -> Optional[str]:
    for enc in ("utf-8-sig", "utf-8", "cp932", "euc_jp", "shift_jis"):
        try:
            content = file_path.read_text(encoding=enc)
            return content[1:] if content.startswith("\ufeff") else content
        except (UnicodeDecodeError, OSError):
            continue
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return content[1:] if content.startswith("\ufeff") else content
    except OSError:
        return None

def build_directory_index_text(
    project_root: Path,
    *,
    include_files: bool = True,
    bundle_format: str = "txt",
    custom_policy_path: Optional[Path] = None,
) -> str:
    """ルート直下ファイルと各ディレクトリの役割・直下ファイルのみを含む軽量インデックスを生成する。"""
    resolved_root = project_root.resolve()
    target_files = get_target_files(resolved_root)
    policy_text = _discover_policy_text(resolved_root, custom_policy_path).strip()

    root_files: List[str] = []
    dir_files: Dict[str, List[str]] = {}
    all_dirs: Set[str] = set()

    for file_path in target_files:
        try:
            rel_path = file_path.relative_to(resolved_root)
        except ValueError:
            continue
        parts = rel_path.parts
        if len(parts) == 1:
            root_files.append(parts[0])
            continue
        dir_rel = rel_path.parent.as_posix()
        dir_files.setdefault(dir_rel, []).append(parts[-1])
        for idx in range(1, len(parts)):
            all_dirs.add("/".join(parts[:idx]))

    root_files.sort(key=str.lower)
    dir_entries: List[str] = []
    for rel_dir in sorted(all_dirs, key=str.lower):
        files_in_dir = sorted(dir_files.get(rel_dir, []), key=str.lower)
        role_desc = _extract_dir_role(resolved_root / rel_dir)
        # 直下ファイルも責務注釈も持たない純粋な中間ディレクトリは省いてトークン消費を抑える
        if not files_in_dir and not role_desc:
            continue
        line = f"{rel_dir}/"
        if role_desc:
            line += f": {role_desc}"
        if files_in_dir:
            if include_files:
                line += f" | files: {', '.join(files_in_dir)}"
            else:
                line += f" ({len(files_in_dir)} files)"
        dir_entries.append(line)

    if bundle_format in ("xml", "txt"):
        lines: List[str] = ["<ai_directory_index>"]
        if policy_text:
            lines.append("  <policy>\n" + policy_text + "\n  </policy>")
        lines.append("  <project_root_files>\n    " + (", ".join(root_files) if root_files else "(none)") + "\n  </project_root_files>")
        lines.append("  <directories>")
        for entry in dir_entries:
            lines.append(f"    {entry}")
        lines.append("  </directories>")
        lines.append("</ai_directory_index>")
        return "\n".join(lines)

    lines = ["# Directory Index"]
    if policy_text:
        lines.append("## Policy\n" + policy_text)
    lines.append("## Project Root Files\n" + (", ".join(root_files) if root_files else "(none)"))
    lines.append("## Directories")
    for entry in dir_entries:
        lines.append(f"- {entry}")
    return "\n".join(lines)

def build_directory_index_file(
    project_root: Path,
    output_dir: Path,
    *,
    include_files: bool = True,
    bundle_format: str = "txt",
    custom_policy_path: Optional[Path] = None,
) -> Path:
    """軽量ディレクトリインデックスを生成して出力ディレクトリへ保存する。"""
    ext = "md" if bundle_format == "markdown" else ("xml" if bundle_format == "xml" else "txt")
    index_path = output_dir / f"directory_index.{ext}"
    output_dir.mkdir(parents=True, exist_ok=True)
    text = build_directory_index_text(
        project_root,
        include_files=include_files,
        bundle_format=bundle_format,
        custom_policy_path=custom_policy_path,
    )
    index_path.write_text(text, encoding="utf-8")
    return index_path

def _build_project_file_lookup(
    resolved_root: Path,
    all_files: Sequence[Path],
) -> Tuple[Dict[str, Path], Dict[str, Path]]:
    rel_map: Dict[str, Path] = {}
    mod_map: Dict[str, Path] = {}
    for fp in all_files:
        try:
            rel_posix = fp.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        rel_map[rel_posix] = fp
        if fp.suffix == ".py":
            mod_name = rel_posix[:-3].replace("/", ".")
            mod_map[mod_name] = fp
            if mod_name.endswith(".__init__"):
                mod_map[mod_name[:-9]] = fp
            if mod_name.startswith("src."):
                short_mod = mod_name[4:]
                mod_map.setdefault(short_mod, fp)
                if short_mod.endswith(".__init__"):
                    mod_map.setdefault(short_mod[:-9], fp)
    return rel_map, mod_map

def _resolve_import_to_project_file(
    import_str: str,
    source_file: Path,
    resolved_root: Path,
    rel_map: Dict[str, Path],
    mod_map: Dict[str, Path],
) -> Optional[Path]:
    clean = (import_str or "").strip()
    if not clean:
        return None
    if clean in mod_map:
        return mod_map[clean]

    # 相対インポートやスラッシュ区切りパス (JS/TS/C++/Dart等) を実ファイルへ解決
    if "/" in clean or clean.startswith("."):
        base_dir = source_file.parent if clean.startswith(".") else resolved_root
        candidates_rel: List[str] = []
        raw_target = (base_dir / clean).resolve()
        if raw_target.is_relative_to(resolved_root):
            base_rel = raw_target.relative_to(resolved_root).as_posix()
            if base_rel != ".":
                candidates_rel.append(base_rel)
            for ext in _SCRIPT_EXT_CANDIDATES:
                if base_rel != ".":
                    candidates_rel.append(f"{base_rel}{ext}")
                    candidates_rel.append(f"{base_rel}/index{ext}")
                else:
                    candidates_rel.append(f"index{ext}")
        for cand in candidates_rel:
            if cand in rel_map:
                return rel_map[cand]

    dotted = clean.replace("/", ".")
    if dotted in mod_map:
        return mod_map[dotted]
    parts = dotted.split(".")
    while len(parts) > 1:
        parts.pop()
        prefix = ".".join(parts)
        if prefix in mod_map:
            return mod_map[prefix]
    return None

def _collect_direct_dependency_files(
    primary_files: Sequence[Path],
    resolved_root: Path,
    all_files: Sequence[Path],
) -> List[Path]:
    rel_map, mod_map = _build_project_file_lookup(resolved_root, all_files)
    primary_set = {p.resolve() for p in primary_files} | set(primary_files)
    dep_files: Set[Path] = set()

    for src_file in primary_files:
        parser = get_parser_for_file(src_file)
        if not parser:
            continue
        content = _read_text_safely(src_file)
        if content is None:
            continue
        try:
            rel_posix = src_file.relative_to(resolved_root).as_posix()
            _, _, dep = parser.parse_and_process(content, rel_posix, set(), set())
        except Exception:
            continue
        # JS/TS・C/C++パーサーはinternal_importsが空でimported_modulesのみ格納するためフォールバック
        imp_candidates = dep.internal_imports if (dep.internal_imports or dep.external_imports) else dep.imported_modules
        for imp in imp_candidates:
            matched = _resolve_import_to_project_file(imp, src_file, resolved_root, rel_map, mod_map)
            if matched is not None and matched not in primary_set and matched.resolve() not in primary_set:
                dep_files.add(matched)

    return sorted(dep_files, key=lambda p: p.relative_to(resolved_root).as_posix().lower())

def resolve_scoped_target_files(
    project_root: Path,
    target_specs: Sequence[str],
    *,
    default_recursive: bool = True,
    include_deps: bool = True,
) -> ScopeResolutionResult:
    """指定ディレクトリ/ファイル群と(設定時は)直接依存ファイルを解決して返す。"""
    resolved_root = project_root.resolve()
    all_files = get_target_files(resolved_root)
    resolved_to_rel_fp: Dict[Path, Path] = {}
    logical_files: List[Path] = []
    for fp in all_files:
        try:
            fp.relative_to(resolved_root)
            logical_files.append(fp)
            resolved_to_rel_fp[fp.resolve()] = fp
        except ValueError:
            continue

    primary_set: Set[Path] = set()
    missing_specs: List[str] = []
    resolved_specs: List[str] = []

    for raw_spec in target_specs:
        spec = (raw_spec or "").strip().strip("\"'`")
        if not spec:
            continue
        spec_norm = spec.replace("\\", "/")
        force_recursive = spec_norm.endswith("/**") or spec_norm.endswith("/*/**")
        if spec_norm.endswith("/*/**"):
            spec_clean = spec_norm[:-5] or "."
        elif spec_norm.endswith("/**"):
            spec_clean = spec_norm[:-3] or "."
        elif spec_norm.endswith("/*"):
            spec_clean = spec_norm[:-2] or "."
        else:
            spec_clean = spec_norm.rstrip("/") or "."

        cand_path = Path(spec_clean)
        try:
            target_path = cand_path.resolve() if cand_path.is_absolute() else (resolved_root / cand_path).resolve()
        except OSError:
            missing_specs.append(spec)
            continue

        if not target_path.is_relative_to(resolved_root):
            missing_specs.append(spec)
            continue

        if target_path.is_file():
            matched_fp = resolved_to_rel_fp.get(target_path)
            if matched_fp is not None:
                primary_set.add(matched_fp)
                spec_label = matched_fp.relative_to(resolved_root).as_posix()
                if spec_label not in resolved_specs:
                    resolved_specs.append(spec_label)
            else:
                missing_specs.append(spec)
            continue

        if target_path.is_dir():
            rel_dir = "." if target_path == resolved_root else target_path.relative_to(resolved_root).as_posix()
            recursive = force_recursive or default_recursive
            direct_matches = {fp for fp in logical_files if fp.parent == target_path}
            if recursive:
                matches = {fp for fp in logical_files if target_path in fp.parents}
            elif direct_matches:
                matches = direct_matches
            elif target_path != resolved_root:
                # 親コンテナディレクトリのみ指定された場合は配下ファイルを救済展開する
                matches = {fp for fp in logical_files if target_path in fp.parents}
            else:
                matches = set()

            if matches:
                primary_set.update(matches)
                spec_label = f"{rel_dir}/**" if force_recursive else rel_dir
                if spec_label not in resolved_specs:
                    resolved_specs.append(spec_label)
            else:
                missing_specs.append(spec)
            continue

        missing_specs.append(spec)

    primary_files = sorted(primary_set, key=lambda p: p.relative_to(resolved_root).as_posix().lower())
    dep_files = _collect_direct_dependency_files(primary_files, resolved_root, logical_files) if include_deps and primary_files else []
    all_scoped = sorted(set(primary_files) | set(dep_files), key=lambda p: p.relative_to(resolved_root).as_posix().lower())

    return ScopeResolutionResult(
        primary_files=primary_files,
        dependency_files=dep_files,
        all_scoped_files=all_scoped,
        missing_specs=missing_specs,
        resolved_specs=resolved_specs,
    )

def _format_extra_non_code_files(
    primary_files: Sequence[Path],
    resolved_root: Path,
    max_chars_per_file: int,
) -> str:
    if max_chars_per_file <= 0:
        return ""
    blocks: List[str] = []
    for fp in primary_files:
        if get_parser_for_file(fp) is not None:
            continue
        content = _read_text_safely(fp)
        if content is None:
            continue
        rel = fp.relative_to(resolved_root).as_posix()
        lang = detect_language(fp)
        text = content.rstrip("\r\n")
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + f"\n... (truncated: aiskel c {rel} で全文取得可能)"
        max_run = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=2)
        fence = "`" * max(3, max_run + 1)
        blocks.append(f"ファイルパス: {rel}\n{fence}{lang}\n{text}\n{fence}")
    return "\n\n".join(blocks)

def build_scoped_bundle_text(
    project_root: Path,
    target_specs: Sequence[str],
    *,
    default_recursive: bool = True,
    include_deps: bool = True,
    include_tree: bool = True,
    bundle_format: str = "txt",
    custom_policy_path: Optional[Path] = None,
    extra_text_max_chars: int = DEFAULT_EXTRA_TEXT_MAX_CHARS,
) -> Tuple[str, ScopeResolutionResult]:
    """指定パス群と直接依存先のみを含むスコープ限定アーキテクチャバンドルを生成する。"""
    resolved_root = project_root.resolve()
    resolution = resolve_scoped_target_files(
        resolved_root,
        target_specs,
        default_recursive=default_recursive,
        include_deps=include_deps,
    )
    if not resolution.all_scoped_files:
        return "", resolution

    all_roles: List[RoleEntry] = []
    all_deps: List[DependencyEntry] = []
    for src_file in resolution.all_scoped_files:
        parser = get_parser_for_file(src_file)
        if not parser:
            continue
        content = _read_text_safely(src_file)
        if content is None:
            continue
        rel_posix = src_file.relative_to(resolved_root).as_posix()
        try:
            _, roles, dep = parser.parse_and_process(content, rel_posix, set(), set())
        except Exception:
            continue
        all_roles.extend(roles)
        all_deps.append(dep)

    policy_text = _discover_policy_text(resolved_root, custom_policy_path).strip()
    role_map_text = generate_role_map_text(all_roles)
    dependency_map_text = generate_dependency_map_text(all_deps)
    tree_text = generate_tree_text(resolved_root, resolution.all_scoped_files) if include_tree else ""
    extra_files_text = _format_extra_non_code_files(resolution.primary_files, resolved_root, extra_text_max_chars)
    scope_attr = ", ".join(resolution.resolved_specs)

    if bundle_format in ("xml", "txt"):
        escaped_scope = html.escape(scope_attr, quote=True)
        lines: List[str] = [f'<ai_architecture_bundle scope="{escaped_scope}">']
        if policy_text:
            lines.append("  <policy>\n" + policy_text + "\n  </policy>")
        if tree_text:
            lines.append("  <project_tree>\n" + tree_text.strip() + "\n  </project_tree>")
        lines.append("  <role_architecture_map>\n" + role_map_text.strip() + "\n  </role_architecture_map>")
        lines.append("  <dependency_graph>\n" + dependency_map_text.strip() + "\n  </dependency_graph>")
        if extra_files_text:
            lines.append("  <scoped_non_code_files>\n" + extra_files_text + "\n  </scoped_non_code_files>")
        lines.append("</ai_architecture_bundle>")
        return "\n".join(lines), resolution

    lines = [f"# Scoped Architecture Bundle ({scope_attr})"]
    if policy_text:
        lines.append("## Policy\n" + policy_text)
    if tree_text:
        lines.append("## Tree\n```\n" + tree_text.strip() + "\n```")
    lines.append(role_map_text.strip())
    lines.append(dependency_map_text.strip())
    if extra_files_text:
        lines.append("## Non-Code Files\n" + extra_files_text)
    return "\n".join(lines), resolution
