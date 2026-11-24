"""Module: @role: AI Studio自動貼り付け用の複数パターン別プロンプト定義・読込・組み立てを担当する。"""
import re
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Union

_D_START = "<" * 4
_D_MID = "=" * 4
_D_END = ">" * 4
_COMMIT_OPEN = "<" + "commit>"
_COMMIT_CLOSE = "</" + "commit>"
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

BUILTIN_DEFAULT_PROMPT = (
    f"{_CORE_ENGINEERING_RULES_HEAD_COMMON}\n\n"
    f"{_DEFAULT_CONTEXT_RULES}\n\n"
    f"{_DEFAULT_OUTPUT_RULES}\n\n"
    f"{_CORE_ENGINEERING_RULES_TAIL}"
)
_BUILTIN_REVIEW_BASE_RULES = (
    f"{_CORE_ENGINEERING_RULES_HEAD_COMMON}\n\n"
    f"{_REVIEW_CONTEXT_RULES}\n\n"
    f"{_REVIEW_PATCH_FORMAT_RULES}\n\n"
    f"{_CORE_ENGINEERING_RULES_TAIL}"
)


def _decode_prompt_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


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
    """対象プロジェクトの設定ファイルまたはビルトインからデフォルトプロンプトを読み込む。"""
    resolved = project_root.resolve()
    candidates = (
        resolved / "PROMPT.txt",
        resolved / "ai_meta" / "PROMPT.txt",
        resolved / ".ai_meta" / "PROMPT.txt",
        resolved / "ai_meta" / "prompts" / "default.txt",
        resolved / ".ai_meta" / "prompts" / "default.txt",
    )
    custom = _read_first_non_empty_file(candidates)
    if custom is not None:
        return custom
    return BUILTIN_DEFAULT_PROMPT


def build_patch_review_prompt(default_prompt: str) -> str:
    """デフォルトプロンプトの規約をベースに新規実装内容の欠陥指摘を依頼するレビュー専用プロンプトを組み立てる。"""
    stripped = (default_prompt or "").strip()
    is_builtin = (not stripped) or (stripped == BUILTIN_DEFAULT_PROMPT.strip())
    if is_builtin:
        base_section = (
            "=== 基本規約（設計・実装・パッチ準拠ルール） ===\n"
            f"{_BUILTIN_REVIEW_BASE_RULES}"
        )
    else:
        sanitized, replaced = _replace_output_flow_section_for_review(stripped)
        header = (
            "=== 基本規約（プロジェクト固有ルール：出力手順は上記「検証手順と出力方針」を優先） ==="
            if replaced
            else "=== 基本規約（プロジェクト固有ルール：出力省略制約・出力例より上記「検証手順と出力方針」とパッチ書式を優先） ==="
        )
        base_section = f"{header}\n{sanitized}"
    return (
        "【依頼タスク: 新規実装・変更内容の厳密コードレビューと欠陥指摘】\n"
        "下記の「基本規約」を厳守した上で、後続の提案実装セクション（`<proposed_implementation>` 等）"
        "および `<ai_architecture_bundle>`（最新のプロジェクト構造・型定義・依存関係）を突き合わせ、"
        "潜在的な欠陥・バグ・設計不整合・規約違反がないか徹底的に検証し指摘せよ。\n\n"
        "=== 検証観点（必須チェック項目） ===\n"
        "1. ロジック欠陥・エッジケース: 境界値・空値・不正入力・例外処理漏れ・パッチ適用時の検索文字列不整合や構文破壊がないか。\n"
        "2. リグレッション・波及影響: 既存のシグネチャ・戻り値型・呼び出し元・依存モジュールへの破壊的影響がないか。\n"
        "3. 状態管理・並行性・リソース: レースコンディション・競合状態・重複実行・タイムアウト・リソースリークがないか。\n"
        "4. セキュリティ・汎用性: 入力検証不備・環境依存（OS/パス/TZ等）・テスト過剰適合（ハードコード）が混入していないか。\n"
        "5. 規約準拠: 下記の基本規約（ファイル肥大化分割、メタデータ、型注釈、Enum/定数集約、Whyコメント最小化、DRY）に違反していないか。\n\n"
        "=== 検証手順と出力方針 ===\n"
        "- 要約バンドルだけでは変更対象の前後文脈や呼び出し元の実装が不明な場合は、推測で断定せず `aiskel c`（`-d` オプション付き1行ワンライナー）で該当コードを要求して確認せよ。\n"
        "- 欠陥や懸念事項を検出した場合は、該当箇所・根本原因・具体的なリスクを指摘し、修正パッチ（`aiskel a` 準拠形式）を提示せよ。\n"
        "- 欠陥が見当たらない場合も、検証した観点と安全性の根拠を簡潔に報告せよ（不要な雑談・挨拶は禁止）。\n\n"
        f"{base_section}"
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
        custom_review = _read_first_non_empty_file(
            (
                resolved / "REVIEW_PROMPT.txt",
                resolved / "ai_meta" / "REVIEW_PROMPT.txt",
                resolved / ".ai_meta" / "REVIEW_PROMPT.txt",
                resolved / "ai_meta" / "prompts" / "patch_review.txt",
                resolved / ".ai_meta" / "prompts" / "patch_review.txt",
            )
        )
        if custom_review is not None:
            return custom_review
        return build_patch_review_prompt(load_default_prompt(resolved))

    raise ValueError(f"未対応のプロンプトパターンです: {pattern}")
