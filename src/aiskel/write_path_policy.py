"""Module: @role: 書き込み可能パスのホワイトリスト・ブラックリスト・無効ポリシー定義およびGlobパス照合を担当する。"""
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

_GLOB_META_CHARS = ("*", "?", "[")

class WritePathMode(str, Enum):
    DISABLED = "disabled"
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"
    BOTH = "both"
    AUTO = "auto"

_MODE_ALIASES: Dict[str, WritePathMode] = {
    "disabled": WritePathMode.DISABLED,
    "disable": WritePathMode.DISABLED,
    "off": WritePathMode.DISABLED,
    "none": WritePathMode.DISABLED,
    "false": WritePathMode.DISABLED,
    "no": WritePathMode.DISABLED,
    "unrestricted": WritePathMode.DISABLED,
    "無効": WritePathMode.DISABLED,
    "whitelist": WritePathMode.WHITELIST,
    "white": WritePathMode.WHITELIST,
    "allow": WritePathMode.WHITELIST,
    "allowlist": WritePathMode.WHITELIST,
    "only": WritePathMode.WHITELIST,
    "ホワイトリスト": WritePathMode.WHITELIST,
    "許可リスト": WritePathMode.WHITELIST,
    "blacklist": WritePathMode.BLACKLIST,
    "black": WritePathMode.BLACKLIST,
    "deny": WritePathMode.BLACKLIST,
    "denylist": WritePathMode.BLACKLIST,
    "block": WritePathMode.BLACKLIST,
    "blocklist": WritePathMode.BLACKLIST,
    "exclude": WritePathMode.BLACKLIST,
    "ブラックリスト": WritePathMode.BLACKLIST,
    "禁止リスト": WritePathMode.BLACKLIST,
    "拒否リスト": WritePathMode.BLACKLIST,
    "both": WritePathMode.BOTH,
    "all": WritePathMode.BOTH,
    "strict": WritePathMode.BOTH,
    "whitelist+blacklist": WritePathMode.BOTH,
    "併用": WritePathMode.BOTH,
    "両方": WritePathMode.BOTH,
    "auto": WritePathMode.AUTO,
    "default": WritePathMode.AUTO,
    "自動": WritePathMode.AUTO,
}

def strip_matching_quotes(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1].strip()
    return s

def normalize_write_path_mode(raw: object) -> Optional[WritePathMode]:
    """設定値からWritePathModeを正規化して返す。"""
    if raw is None:
        return None
    if isinstance(raw, WritePathMode):
        return raw
    if isinstance(raw, bool):
        return WritePathMode.AUTO if raw else WritePathMode.DISABLED
    if isinstance(raw, str):
        cleaned = strip_matching_quotes(raw).strip().lower()
        if not cleaned:
            return None
        mode = _MODE_ALIASES.get(cleaned)
        if mode is None:
            print(
                f"⚠ 未知の write_paths_mode 指定です: '{raw}' (auto として扱います)",
                file=sys.stderr,
            )
            return WritePathMode.AUTO
        return mode
    return None

def resolve_allowed_write_paths(
    raw_paths: Sequence[Union[str, Path]],
    project_root: Path,
) -> List[Path]:
    """設定された書き込み許可パス群をproject_root基準の解決済みPathリストへ正規化する。"""
    base_dir = project_root.resolve()
    resolved_list: List[Path] = []
    seen: Set[Path] = set()
    for raw in raw_paths:
        if isinstance(raw, Path):
            p = raw
        else:
            cleaned = strip_matching_quotes(str(raw))
            if not cleaned:
                continue
            p = Path(cleaned)
        resolved = p.resolve() if p.is_absolute() else (base_dir / p).resolve()
        if resolved not in seen:
            seen.add(resolved)
            resolved_list.append(resolved)
    return resolved_list

def is_write_path_allowed(target_path: Path, allowed_paths: Sequence[Path]) -> bool:
    """対象ファイルパスが書き込み許可パス群のいずれかに該当・包含されるか判定する。"""
    if not allowed_paths:
        return True
    resolved_target = target_path.resolve()
    for allowed in allowed_paths:
        resolved_allowed = allowed.resolve()
        if resolved_target == resolved_allowed:
            return True
        # なぜ必要か: 実在する単一ファイルと同名プレフィックス配下への誤許可を防ぐため
        if resolved_allowed.is_file():
            continue
        if resolved_allowed in resolved_target.parents:
            return True
    return False

def _has_glob_chars(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_META_CHARS)

def _compile_glob_regex(
    pattern: str,
    allow_dir_descendants: bool = False,
    allow_dir_exact: bool = True,
) -> re.Pattern[str]:
    i = 0
    n = len(pattern)
    parts: List[str] = ["^"]
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    parts.append("(?:.+/)?")
                    i += 3
                    continue
                parts.append(".*")
                i += 2
                continue
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        elif ch == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                parts.append(re.escape(ch))
            else:
                cls_body = pattern[i + 1 : j]
                if cls_body.startswith("!"):
                    cls_body = "^" + cls_body[1:]
                parts.append(f"[{cls_body}]")
                i = j
        else:
            parts.append(re.escape(ch))
        i += 1
    if allow_dir_descendants:
        parts.append("(?:/.*)?" if allow_dir_exact else "/.*")
    parts.append("$")
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    try:
        return re.compile("".join(parts), flags)
    except re.error:
        # なぜ必要か: 不正な文字クラス([...])を含む設定値でパッチ適用がクラッシュするのを防ぐため
        fallback_parts = ["^", re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")]
        if allow_dir_descendants:
            fallback_parts.append("(?:/.*)?" if allow_dir_exact else "/.*")
        fallback_parts.append("$")
        return re.compile("".join(fallback_parts), flags)

def _match_write_pattern(
    resolved_target: Path,
    rel_posix: Optional[str],
    raw_pattern: str,
    base_dir: Path,
) -> bool:
    cleaned = strip_matching_quotes(raw_pattern)
    if not cleaned:
        return False

    norm = cleaned.replace("\\", "/")
    if not _has_glob_chars(norm):
        p = Path(cleaned)
        resolved_pat = p.resolve() if p.is_absolute() else (base_dir / p).resolve()
        if resolved_target == resolved_pat:
            return True
        # なぜ必要か: 実在する単一ファイルと同名プレフィックス配下への誤許可・誤拒否を防ぐため
        if resolved_pat.is_file():
            return False
        return resolved_pat in resolved_target.parents

    is_dir_glob = norm.endswith("/")
    pat_body = norm.rstrip("/")
    if not pat_body:
        return False

    allow_dir_exact = resolved_target.is_dir()

    if Path(cleaned).is_absolute():
        abs_pat = Path(pat_body).as_posix()
        return bool(
            _compile_glob_regex(
                abs_pat,
                allow_dir_descendants=is_dir_glob and not abs_pat.endswith("/**"),
                allow_dir_exact=allow_dir_exact,
            ).match(resolved_target.as_posix())
        )

    if pat_body.startswith("./"):
        pat_body = pat_body[2:]
        if not pat_body or rel_posix is None:
            return False
        return bool(
            _compile_glob_regex(
                pat_body,
                allow_dir_descendants=is_dir_glob and pat_body != "**" and not pat_body.endswith("/**"),
                allow_dir_exact=allow_dir_exact,
            ).match(rel_posix)
        )

    if "/" not in pat_body:
        if not is_dir_glob:
            # なぜ必要か: 相対Globでプロジェクトルート外が誤許可されるのを防ぐため
            if rel_posix is None:
                return False
            return bool(_compile_glob_regex(pat_body).match(resolved_target.name))
        if rel_posix is None:
            return False
        if pat_body == "**":
            return True
        dir_re = _compile_glob_regex(pat_body, allow_dir_descendants=False)
        segments = rel_posix.split("/") if allow_dir_exact else rel_posix.split("/")[:-1]
        return any(bool(dir_re.match(seg)) for seg in segments)

    if rel_posix is None:
        return False
    return bool(
        _compile_glob_regex(
            pat_body,
            allow_dir_descendants=is_dir_glob and not pat_body.endswith("/**"),
            allow_dir_exact=allow_dir_exact,
        ).match(rel_posix)
    )

@dataclass(frozen=True)
class WritePathPolicy:
    """書き込み可能パスのホワイトリスト・ブラックリスト・無効ポリシーを保持・判定する。"""

    project_root: Path
    mode: WritePathMode = WritePathMode.AUTO
    allow_patterns: Tuple[str, ...] = ()
    deny_patterns: Tuple[str, ...] = ()

    @classmethod
    def from_allowed_paths(
        cls,
        raw_paths: Sequence[Union[str, Path]],
        project_root: Path,
        mode: WritePathMode = WritePathMode.AUTO,
    ) -> "WritePathPolicy":
        patterns: List[str] = []
        seen: Set[str] = set()
        for raw in raw_paths:
            text = str(raw) if isinstance(raw, Path) else strip_matching_quotes(str(raw))
            if text and text not in seen:
                seen.add(text)
                patterns.append(text)
        return cls(
            project_root=project_root.resolve(),
            mode=mode,
            allow_patterns=tuple(patterns),
            deny_patterns=(),
        )

    @property
    def is_active(self) -> bool:
        if self.mode == WritePathMode.DISABLED:
            return False
        if self.mode in (WritePathMode.WHITELIST, WritePathMode.BLACKLIST, WritePathMode.BOTH):
            return True
        return bool(self.allow_patterns or self.deny_patterns)

    @property
    def allow_paths(self) -> List[Path]:
        return resolve_allowed_write_paths(self.allow_patterns, self.project_root)

    @property
    def deny_paths(self) -> List[Path]:
        return resolve_allowed_write_paths(self.deny_patterns, self.project_root)

    @property
    def has_whitelist_rule(self) -> bool:
        if self.mode in (WritePathMode.WHITELIST, WritePathMode.BOTH):
            return True
        return self.mode == WritePathMode.AUTO and bool(self.allow_patterns)

    @property
    def has_blacklist_rule(self) -> bool:
        if self.mode in (WritePathMode.BLACKLIST, WritePathMode.BOTH):
            return True
        return self.mode == WritePathMode.AUTO and bool(self.deny_patterns)

    def is_explicitly_denied(self, target_path: Path) -> Tuple[bool, str]:
        """対象パスがブラックリスト設定により明示的に禁止されているか返す。"""
        if self.mode == WritePathMode.DISABLED:
            return False, ""
        if self.has_blacklist_rule and self._matches_any(target_path, self.deny_patterns):
            return True, "ai_meta設定の denied_write_paths (ブラックリスト) により禁止されているパスです"
        return False, ""

    def _matches_any(self, target_path: Path, patterns: Tuple[str, ...]) -> bool:
        if not patterns:
            return False
        base_dir = self.project_root.resolve()
        resolved_target = (
            target_path.resolve()
            if target_path.is_absolute()
            else (base_dir / target_path).resolve()
        )
        try:
            rel_posix: Optional[str] = resolved_target.relative_to(base_dir).as_posix()
        except ValueError:
            rel_posix = None
        return any(
            _match_write_pattern(resolved_target, rel_posix, pat, base_dir)
            for pat in patterns
        )

    def check_path(self, target_path: Path) -> Tuple[bool, str]:
        """対象パスへの書き込み可否と拒否理由を返す。"""
        if self.mode == WritePathMode.DISABLED:
            return True, ""

        if self.mode == WritePathMode.WHITELIST:
            if not self.allow_patterns:
                return False, "ai_meta設定の allowed_write_paths (ホワイトリスト) が空のため許可されていないパスです"
            if self._matches_any(target_path, self.allow_patterns):
                return True, ""
            return False, "ai_meta設定の allowed_write_paths (ホワイトリスト) により許可されていないパスです"

        if self.mode == WritePathMode.BLACKLIST:
            if self._matches_any(target_path, self.deny_patterns):
                return False, "ai_meta設定の denied_write_paths (ブラックリスト) により禁止されているパスです"
            return True, ""

        if self.mode == WritePathMode.BOTH:
            if self._matches_any(target_path, self.deny_patterns):
                return False, "ai_meta設定の denied_write_paths (ブラックリスト) により禁止されているパスです"
            if not self.allow_patterns:
                return False, "ai_meta設定の allowed_write_paths (ホワイトリスト) が空のため許可されていないパスです"
            if self._matches_any(target_path, self.allow_patterns):
                return True, ""
            return False, "ai_meta設定の allowed_write_paths (ホワイトリスト) により許可されていないパスです"

        # AUTOモード: 非空リストを適用し、両方指定時はブラックリスト優先
        if self.deny_patterns and self._matches_any(target_path, self.deny_patterns):
            return False, "ai_meta設定の denied_write_paths (ブラックリスト) により禁止されているパスです"
        if self.allow_patterns and not self._matches_any(target_path, self.allow_patterns):
            return False, "ai_meta設定の allowed_write_paths により許可されていないパスです"
        return True, ""

    def is_allowed(self, target_path: Path) -> bool:
        allowed, _ = self.check_path(target_path)
        return allowed
