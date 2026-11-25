"""Module: @role: AI Studio自動貼り付け用の複数パターン別プロンプト定義・読込・組み立てを担当する。"""
import re
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Union
import importlib.resources as importlib_resources
from typing import Dict, List, Optional, Tuple, Union

_D_START = "<" * 4
_D_MID = "=" * 4
_D_END = ">" * 4
_COMMIT_OPEN = "<" + "commit>"
_COMMIT_CLOSE = "</" + "commit>"
PATCH_HANDOFF_STOP_SIGNAL = "[AISKEL-NO-DEFECTS]"
_OUTPUT_FLOW_SECTION_RE = re.compile(
    r"^(?P<indent>[ \t]*)2\.[ \t]*コード修正・出力フロー[^\n]*\n[\s\S]*?(?=^(?P=indent)3\.[ \t]|\Z)",
    re.MULTILINE,
)


class PromptPattern(str, Enum):
    """用途別の自動貼り付けプロンプトパターン種別を定義する。"""

    DEFAULT = "default"
    PATCH_REVIEW = "patch_review"


_CORE_ENGINEERING_RULES_HEAD_COMMON = """Role: エキスパートソフトウェアエンジニア。以下を厳守せよ。

0. トラブルシューティングと根本解決
- 仕様縮小・回避禁止。要求要件は必ず成立させる。
- 表層的エラー回避ではなく、根本原因を分析し解消する。
- リグレッション完全防止: 過去に解消した問題（A）と新たな問題（B）を両立させる。両立・根本解決のためであれば、過去の実装や静的定義・インターフェースの刷新を厭わない。
- 【テスト過剰適合（テストハック）厳禁】: テストケースの入力値・特定データのみを通すハードコーディングや、テスト環境特有の前提（固定パス、特定モック依存など）に甘えた実装は厳禁。実環境・本番運用を前提とし、仕様・ドメインモデルに沿った汎用的かつ堅牢なロジックを組むこと。"""

_DEFAULT_CONTEXT_RULES = """1. コンテキスト解釈とトークン最小化規則
- <static_skeleton>: 静的ルール・型定義（原則維持だが、根本解決・リグレッション完全防止に必要な場合は変更・拡張可能）。
- <dynamic_flesh>: 現在の作業対象。
- ...: 既存実装の省略箇所。
- aiskel バンドル (<ai_architecture_bundle>) の活用:
  - `role_architecture_map` 内の型シグネチャ、フィールド、Enum、定数は正確な正本として扱う（根本解決に伴い修正が必要な場合はパッチ対象に含める）。
  - 【原則Zero-Shot実装】: 型定義やシグネチャ、docstringから振る舞いが推測可能な場合はコード要求をスキップし、即座に修正パッチを出力せよ。
  - 【ワンショット一括要求（無計画な細切れラリー厳禁）】:
    - 取得コードの未確認による安易な追加要求を禁じ、初手で必要な情報を網羅せよ。
    - 依存自動追従フラグ `-d` の活用: 内部ロジックや関連ヘルパーの確認が必要な場合は必ず `-d` を付与し、同一ファイル内の依存ノードを一括展開せよ（例: `aiskel c -d path/a.py:funcA`）。
    - 粒度に応じた一括指定: 変更範囲が広い場合は、個別列挙を避けファイル単位（`aiskel c path/a.py`）またはクラス単位（`aiskel c path/a.py:ClassName`）で要求せよ。
  - 【新事実発覚時の追加要求許可（例外規定）】:
    - 提供コードを精査した結果、「未想定の潜在バグ」「別モジュールへの破壊的波及」「設計上の致命的乖離」などの新事実が発覚した場合は、追加の `aiskel c` を許可する。
    - その際は「コード精査で新たに判明した理由（1行）」を必ず明記せよ（推測や思いつきによる追加は不可）。
  - 【1行ワンライナー厳守】:
    - 初回・追加を問わず、コード要求コマンドは必ず1回・1行に集約せよ（複数行への分割出力は厳禁）。
      - 同一ファイル内: カンマ区切り（例: `aiskel c -d path/a.py:funcA,funcB`）
      - 複数ファイル混在: スペース区切り（例: `aiskel c -d path/a.py:funcA path/b.py:ClassB`）
  - 【差分スコープ (-g)】: コンテキストが `aiskel -g` で抽出された変更・直接依存ファイルのみの最小スコープであることを意識し、未提示コードへの不要な要求を排してスコープ内で完結せよ。"""

_REVIEW_CONTEXT_RULES = """1. コンテキスト解釈とトークン最小化規則
- <static_skeleton>: 静的ルール・型定義（原則維持だが、根本解決・リグレッション完全防止に必要な場合は変更・拡張可能）。
- <dynamic_flesh>: 現在の作業対象。
- ...: 既存実装の省略箇所。
- aiskel バンドル (<ai_architecture_bundle>) の活用:
  - `role_architecture_map` 内の型シグネチャ、フィールド、Enum、定数は正確な正本として扱う。
  - 内部ロジックや関連ヘルパーの確認が必要な場合は、`-d` オプション付き1行ワンライナーの `aiskel c` コマンドで必要対象を過不足なく一括要求せよ。"""

_DEFAULT_OUTPUT_RULES = f"""2. コード修正・出力フロー（トークン効率重視）
- 【出力トークン節約の鉄則】: 挨拶・社交辞令・締めの挨拶・コードブロック外の長文解説は一切禁止。
  - コード要求時: 理由（1行：新事実判明時はその根拠）＋ 1行の `aiskel c` コマンドブロックのみ出力。
  - パッチ提示時: 「原因と根本解決策（箇条書き各1〜2行）」＋ パッチブロックのみ出力。
- 自動適用フォーマット (aiskel a 準拠):
  - コードブロック内部にファイル名コメント記述禁止（`ファイルパス: ...` および `{_COMMIT_OPEN}...{_COMMIT_CLOSE}` のみ）。
  - ネスト崩れ防止のため外枠バッククォートの階層化を徹底せよ。
  - **モード選択基準 (トークン浪費防止)**:
    - 原則は【モードA】: 数行〜十数行の修正は必ずモードAを用い、関数全体の無駄な出力を防ぐこと。
    - 【モードB】の限定適用: 関数の全面書き換え、新規作成、ファイル全体置換時のみ使用。

    【モードA: ピンポイント差分置換・部分削除】
    - `{_D_START}` と `{_D_MID}` の間: 変更対象のコード。一意特定のため前後1〜2行を含め、省略記号（`...`）は厳禁。
    - `{_D_MID}` と `{_D_END}` の間: 変更後コード。コード削除時は完全に空行にする。

    【モードB: 関数・クラス丸ごと全置換 / 新規作成】
    - `{_D_START}` と `{_D_MID}` の間: 完全に空行。
    - `{_D_MID}` と `{_D_END}` の間: 置換後の関数・クラスの完全なコード。

    【モードC: 関数・クラス丸ごと削除】
    - `{_D_START}` と `{_D_MID}` の間に対象のシグネチャ行のみ記述し、`{_D_MID}` と `{_D_END}` の間を完全に空行にする。

    【モードD: ファイル丸ごと削除】
    - `削除: path/to/file.ts` または `ファイルパス: path/to/file.ts (削除)` と記述。

    - 強制置換 (-f) と自動テスト検証 (-T):
      - 適用済みスキップを無視して強制置換したい場合は `aiskel a -f` を案内せよ。
      - パッチ適用後のテスト検証および失敗時ロールバックを行いたい場合は `aiskel a -T`（併用時: `aiskel a -f -T`）を案内せよ。
    - コミットタグ: コードブロック内最末尾に必ず `{_COMMIT_OPEN}Conventional Commits形式(50字以内){_COMMIT_CLOSE}` を配置。

【コード要求時の出力フォーマット例（必ず1行のワンライナー）】
```bash
aiskel c -d path/a.py:funcA path/b.py:ClassB
```

【パッチ提示時の出力フォーマット例】
````typescript
ファイルパス: path/to/file.ts
{_D_START}
  // モードA: 前後行を含めて一意化して置換
  const value = oldValue;
{_D_MID}
  const value = newValue;
{_D_END}
{_D_START}
  // モードC: シグネチャのみ指定して丸ごと削除
  export function deprecatedFunc() {{
{_D_MID}
{_D_END}
ファイルパス: path/to/new_file.ts
{_D_START}
{_D_MID}
// モードB: 空行から始めて新規作成・全置換
export const newUtil = () => true;
{_D_END}
削除: path/to/obsolete.ts

{_COMMIT_OPEN}
refactor: streamline value handling and drop obsolete logic
{_COMMIT_CLOSE}
````"""

_REVIEW_PATCH_FORMAT_RULES = f"""2. 修正パッチ出力フォーマット (aiskel a 準拠)
- 修正パッチを提示する場合のみ以下を厳守せよ:
  - 提示するパッチは、旧タブの提案パッチが未適用である現行コードベースに対して単体でそのまま `aiskel a` 適用できる完全版統合パッチとする（`<<<<` 側には必ず適用前の現行コードを指定し、旧タブのパッチに対する追加差分にしないこと）。
  - コードブロック内部にファイル名コメント記述禁止（`ファイルパス: ...` および `{_COMMIT_OPEN}...{_COMMIT_CLOSE}` のみ）。
  - 数行〜十数行の修正は必ずモードA（前後1〜2行を含めて一意特定、`...` 禁止）を用い、全面書き換え・新規作成のみモードB（`{_D_START}` と `{_D_MID}` の間を空行）、丸ごと削除はモードCまたは `削除: <path>` を用いる。
  - 外枠バッククォートを内部コードより多くし、最末尾に `{_COMMIT_OPEN}Conventional Commits形式(50字以内){_COMMIT_CLOSE}` を配置する。"""

_CORE_ENGINEERING_RULES_TAIL = """3. 設計・分割と aiskel メタデータ規約
- ファイル肥大化（目安500行）時は自律的に別ファイルへ分割せよ。
- 新規・分割作成時の記述規約:
  1. ファイル冒頭: `\"\"\"Module: @role: 【責務の簡潔な自然文】\"\"\"`
  2. ディレクトリ作成時: 直下に `__init__.py` を置きレイヤー責務を記述。
  3. 型注釈: 公開フィールド・引数・戻り値に明示。非公開は `_` プレフィックスを付与。
  4. 状態・定数: `Enum` および大文字スネークケース定数へ集約。

4. 品質・汎用性・コメント
- 【Whyコメントの最小化】: 処理内容の直訳コメントは厳禁。エッジケース対策・再発防止コードのみ、その理由（Why）をコード直前に1行・40字程度で簡潔に記述せよ。
- 【環境非依存・汎用性の徹底】:
  - OS固有パス表現、固定カレントディレクトリ、特定のTZ・ロケールへの依存を排除せよ。
  - プロダクションコード内にテスト用フラグやテスト環境専用の条件分岐を混入させるな。
  - 外部環境・インフラ差分（DB、外部API、ファイルストレージ、時間等）は直接依存せず、引数・設定オブジェクト・インターフェース経由で注入可能（DI）な汎用設計とせよ。
- DRY原則、明確な命名、ボーイスカウトルール徹底。"""

BUILTIN_DEFAULT_PROMPT: str = _load_builtin_sections()["default"]
_BUILTIN_REVIEW_BASE_RULES = (
    f"{_CORE_ENGINEERING_RULES_HEAD_COMMON}\n\n"
    f"{_REVIEW_CONTEXT_RULES}\n\n"
    f"{_REVIEW_PATCH_FORMAT_RULES}\n\n"
    f"{_CORE_ENGINEERING_RULES_TAIL}"
)


def _decode_prompt_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n").strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()

def _read_first_non_empty_file(candidates: Tuple[Path, ...]) -> Optional[str]:
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        text = _decode_prompt_bytes(raw)
        if text:
            return text
    return None


def _replace_output_flow_section_for_review(prompt_text: str) -> Tuple[str, bool]:
    # カスタムPROMPT.txt内の実装用出力省略規約・ダミー例示がレビュー指示と矛盾するのを防ぐ
    if _OUTPUT_FLOW_SECTION_RE.search(prompt_text):
        replaced = _OUTPUT_FLOW_SECTION_RE.sub(
            lambda _m: f"{_REVIEW_PATCH_FORMAT_RULES}\n\n",
            prompt_text,
            count=1,
        )
        return replaced.strip(), True
    return prompt_text.strip(), False


def load_default_prompt(project_root: Path) -> str:
    """対象プロジェクトの設定ファイルまたは外部プロンプト定義ファイルからデフォルトプロンプトを読み込む。"""
    resolved = project_root.resolve()
    custom = _read_first_plain_or_section(
        resolved,
        _DEFAULT_PROMPT_FILENAMES + _UNIFIED_PROMPT_FILENAMES,
        PromptPattern.DEFAULT.value,
    )
    if custom is not None:
        return custom
    return _load_builtin_sections()["default"]

def build_patch_review_prompt(
    default_prompt: str,
    project_root: Optional[Path] = None,
    review_template_override: Optional[str] = None,
) -> str:
    """デフォルトプロンプトの規約をベースに新規実装内容の欠陥指摘を依頼するレビュー専用プロンプトを組み立てる。"""
    builtin_sections = _load_builtin_sections()
    project_sections = _load_project_sections(project_root)

    review_template = (
        review_template_override
        or project_sections.get("patch_review")
        or builtin_sections["patch_review"]
    )
    review_context_rules = (
        project_sections.get("review_context_rules") or builtin_sections["review_context_rules"]
    )
    review_patch_format_rules = (
        project_sections.get("review_patch_format_rules") or builtin_sections["review_patch_format_rules"]
    )

    stripped = (default_prompt or "").strip()
    builtin_default = builtin_sections["default"].strip()
    is_builtin = (
        (not stripped)
        or (stripped == builtin_default)
        or (stripped == BUILTIN_DEFAULT_PROMPT.strip())
    )

    if is_builtin:
        base_rules, _ = _replace_sections_for_review(
            builtin_default,
            review_context_rules,
            review_patch_format_rules,
            replace_context=True,
        )
        base_section = f"=== 基本規約（設計・実装・パッチ準拠ルール） ===\n{base_rules}"
    else:
        sanitized, replaced = _replace_sections_for_review(
            stripped,
            review_context_rules,
            review_patch_format_rules,
            replace_context=False,
        )
        header = (
            "=== 基本規約（プロジェクト固有ルール：出力手順は上記「検証手順と出力方針」を優先） ==="
            if replaced
            else "=== 基本規約（プロジェクト固有ルール：出力省略制約・出力例より上記「検証手順と出力方針」とパッチ書式を優先） ==="
        )
        base_rules = sanitized
        base_section = f"{header}\n{sanitized}"

    return _render_template(
        review_template,
        {
            "{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL,
            "{{BASE_SECTION}}": base_section,
            "{{BASE_RULES}}": base_rules,
        },
    )

def list_prompt_patterns() -> Tuple[str, ...]:
    """利用可能なプロンプトパターン名一覧を返す。"""
    return tuple(p.value for p in PromptPattern)


def load_prompt_for_pattern(project_root: Path, pattern: Union[PromptPattern, str]) -> str:
    """指定されたパターンに対応する専用プロンプト文字列を返す。"""
    raw_value = pattern.value if isinstance(pattern, PromptPattern) else str(pattern or "").strip().lower()
    if not raw_value or raw_value == PromptPattern.DEFAULT.value:
        return load_default_prompt(project_root)

    resolved = project_root.resolve()
    if raw_value == PromptPattern.PATCH_REVIEW.value:
        custom_review = _read_first_plain_or_section(
            resolved,
            _REVIEW_PROMPT_FILENAMES,
            PromptPattern.PATCH_REVIEW.value,
        )
        if custom_review is not None:
            if "{{BASE_SECTION}}" in custom_review or "{{BASE_RULES}}" in custom_review:
                return build_patch_review_prompt(
                    load_default_prompt(resolved),
                    project_root=resolved,
                    review_template_override=custom_review,
                )
            return _render_template(
                custom_review,
                {"{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL},
            )
        project_sections = _load_project_sections(resolved)
        unified_review = project_sections.get(PromptPattern.PATCH_REVIEW.value)
        if unified_review is not None:
            if "{{BASE_SECTION}}" in unified_review or "{{BASE_RULES}}" in unified_review:
                return build_patch_review_prompt(
                    load_default_prompt(resolved),
                    project_root=resolved,
                    review_template_override=unified_review,
                )
            return _render_template(
                unified_review,
                {"{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL},
            )
        return build_patch_review_prompt(load_default_prompt(resolved), project_root=resolved)

    raise ValueError(f"未対応のプロンプトパターンです: {pattern}")

def build_file_split_prompt(
    default_prompt: str,
    target_rel_path: str,
    lines: int,
    chars: int,
    max_lines: int,
    max_chars: int,
    file_index: int = 1,
    total_files: int = 1,
    project_root: Optional[Path] = None,
) -> str:
    """肥大化した単一ファイルを責務ごとに分割するための専用プロンプトを組み立てる。"""
    builtin_sections = _load_builtin_sections()
    project_sections = _load_project_sections(project_root)
    split_template = project_sections.get("file_split") or builtin_sections["file_split"]

    stripped = (default_prompt or "").strip()
    base_rules = stripped if stripped else builtin_sections["default"]
    return _render_template(
        split_template,
        {
            "{{TARGET_REL_PATH}}": target_rel_path,
            "{{LINES}}": str(lines),
            "{{CHARS}}": f"{chars:,}",
            "{{MAX_LINES}}": str(max_lines),
            "{{MAX_CHARS}}": f"{max_chars:,}",
            "{{FILE_INDEX}}": str(file_index),
            "{{TOTAL_FILES}}": str(total_files),
            "{{BASE_RULES}}": base_rules,
        },
    )
_PROMPT_FILE_NAME = "prompts.txt"


_REQUIRED_BUILTIN_KEYS = (
    "default",
    "patch_review",
    "review_context_rules",
    "review_patch_format_rules",
    "file_split",
)


_UNIFIED_PROMPT_FILENAMES = (
    "PROMPTS.txt",
    "PROMPTS.md",
    "ai_meta/PROMPTS.txt",
    "ai_meta/PROMPTS.md",
    ".ai_meta/PROMPTS.txt",
    ".ai_meta/PROMPTS.md",
)


_DEFAULT_PROMPT_FILENAMES = (
    "PROMPT.txt",
    "ai_meta/PROMPT.txt",
    ".ai_meta/PROMPT.txt",
    "ai_meta/prompts/default.txt",
    ".ai_meta/prompts/default.txt",
)


_REVIEW_PROMPT_FILENAMES = (
    "REVIEW_PROMPT.txt",
    "ai_meta/REVIEW_PROMPT.txt",
    ".ai_meta/REVIEW_PROMPT.txt",
    "ai_meta/prompts/patch_review.txt",
    ".ai_meta/prompts/patch_review.txt",
)


_PROMPT_SECTION_HEADER_RE = re.compile(
    r"^[ \t]*===[ \t]*\[PROMPT:[ \t]*([a-zA-Z0-9_]+)[ \t]*\][^\n]*===[ \t]*$"
)


_CONTEXT_RULES_SECTION_RE = re.compile(
    r"^(?P<indent>[ \t]*)1\.[ \t]*コンテキスト解釈とトークン最小化規則[^\n]*\n[\s\S]*?(?=^(?P=indent)2\.[ \t]|\Z)",
    re.MULTILINE,
)


_TOKEN_UNESCAPE_MAP: Tuple[Tuple[str, str], ...] = (
    ("{{<<<<}}", "<" * 4),
    ("{{====}}", "=" * 4),
    ("{{>>>>}}", ">" * 4),
    ("{{FILE_HEADER}}", "ファイル" + "パス:"),
    ("{{DELETE_HEADER}}", "削" + "除:"),
    ("{{COMMIT_OPEN}}", "<" + "commit>"),
    ("{{COMMIT_CLOSE}}", "</" + "commit>"),
)


def _unescape_prompt_tokens(text: str) -> str:
    resolved = text
    for token, literal in _TOKEN_UNESCAPE_MAP:
        if token in resolved:
            resolved = resolved.replace(token, literal)
    return resolved


def _render_template(template: str, replacements: Dict[str, str]) -> str:
    if not replacements:
        return template
    # 置換後の値に含まれる別プレースホルダへの再代入を防ぐため1パスで置換する
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], template)


def _parse_prompt_sections(raw_text: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []

    for line in raw_text.splitlines():
        header_match = _PROMPT_SECTION_HEADER_RE.match(line)
        if header_match:
            if current_key is not None:
                body = _unescape_prompt_tokens("\n".join(buffer).strip())
                if body:
                    sections[current_key] = body
            current_key = header_match.group(1).strip().lower()
            buffer = []
            continue
        if current_key is not None:
            buffer.append(line)

    if current_key is not None:
        body = _unescape_prompt_tokens("\n".join(buffer).strip())
        if body:
            sections[current_key] = body
    return sections


def _read_prompt_file_bytes(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = _decode_prompt_bytes(raw)
    return text if text else None


def _read_builtin_prompt_file_text() -> str:
    try:
        resource = importlib_resources.files("aiskel").joinpath(_PROMPT_FILE_NAME)
        raw = resource.read_bytes()
        text = _decode_prompt_bytes(raw)
        if text:
            return text
    except (ModuleNotFoundError, FileNotFoundError, OSError, TypeError, AttributeError):
        pass

    fallback_path = Path(__file__).resolve().parents[1] / _PROMPT_FILE_NAME
    fallback_text = _read_prompt_file_bytes(fallback_path)
    if fallback_text:
        return fallback_text
    raise FileNotFoundError(f"組込プロンプト定義ファイルが見つかりません: {_PROMPT_FILE_NAME}")


def _load_builtin_sections() -> Dict[str, str]:
    raw_text = _read_builtin_prompt_file_text()
    sections = _parse_prompt_sections(raw_text)
    missing = [key for key in _REQUIRED_BUILTIN_KEYS if not sections.get(key)]
    if missing:
        raise RuntimeError(f"組込プロンプト定義ファイルに必須セクションが不足しています: {', '.join(missing)}")
    return sections


def _load_project_sections(project_root: Optional[Path]) -> Dict[str, str]:
    if project_root is None:
        return {}
    resolved = project_root.resolve()
    merged: Dict[str, str] = {}
    for rel_path in _UNIFIED_PROMPT_FILENAMES + _DEFAULT_PROMPT_FILENAMES:
        raw_text = _read_prompt_file_bytes(resolved / rel_path)
        if not raw_text:
            continue
        sections = _parse_prompt_sections(raw_text)
        if sections:
            for key, val in sections.items():
                merged.setdefault(key, val)
    return merged


def _read_first_plain_or_section(
    project_root: Path,
    rel_paths: Tuple[str, ...],
    section_key: str,
) -> Optional[str]:
    resolved = project_root.resolve()
    for rel_path in rel_paths:
        raw_text = _read_prompt_file_bytes(resolved / rel_path)
        if not raw_text:
            continue
        sections = _parse_prompt_sections(raw_text)
        if sections:
            value = sections.get(section_key)
            if value:
                return value
            continue
        return _unescape_prompt_tokens(raw_text)
    return None


def _replace_sections_for_review(
    prompt_text: str,
    review_context_rules: str,
    review_patch_format_rules: str,
    replace_context: bool,
) -> Tuple[str, bool]:
    updated = prompt_text.strip()
    if replace_context and _CONTEXT_RULES_SECTION_RE.search(updated):
        updated = _CONTEXT_RULES_SECTION_RE.sub(
            lambda _m: f"{review_context_rules.strip()}\n\n",
            updated,
            count=1,
        )

    if _OUTPUT_FLOW_SECTION_RE.search(updated):
        updated = _OUTPUT_FLOW_SECTION_RE.sub(
            lambda _m: f"{review_patch_format_rules.strip()}\n\n",
            updated,
            count=1,
        )
        return updated.strip(), True
    return updated.strip(), False
