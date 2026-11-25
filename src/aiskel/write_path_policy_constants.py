"""Module: @role: 書き込みパス制御の設定キー群・候補ファイル名およびサンプル設定テンプレートを定義する。"""

WRITE_PATHS_SAMPLE_FILENAME = "writable_paths.sample.txt"

ALLOWED_WRITE_PATHS_KEYS = (
    "allowed_write_paths",
    "writable_paths",
    "write_allow_paths",
    "allow_write_paths",
    "write_paths",
    "write_whitelist",
    "writable_whitelist",
    "whitelist_write_paths",
)

DENIED_WRITE_PATHS_KEYS = (
    "denied_write_paths",
    "disallowed_write_paths",
    "blocked_write_paths",
    "forbidden_write_paths",
    "write_deny_paths",
    "deny_write_paths",
    "readonly_paths",
    "read_only_paths",
    "write_blacklist",
    "writable_blacklist",
    "blacklist_write_paths",
)

WRITE_PATH_MODE_KEYS = (
    "write_paths_mode",
    "write_path_mode",
    "write_policy_mode",
    "writable_paths_mode",
    "write_mode",
)

WRITE_PATH_ENABLED_KEYS = (
    "write_paths_enabled",
    "enable_write_paths",
    "write_path_restriction",
)

ALLOWED_WRITE_PATH_TXT_FILES = (
    "allowed_write_paths.txt",
    "writable_paths.txt",
    "allow_write_paths.txt",
)

DENIED_WRITE_PATH_TXT_FILES = (
    "denied_write_paths.txt",
    "deny_write_paths.txt",
    "disallowed_write_paths.txt",
    "blocked_write_paths.txt",
    "readonly_paths.txt",
)

META_CANDIDATE_CONFIG_FILES = (
    "config.json",
    "settings.json",
    "aiskel.json",
    "aiskel_config.json",
    ".aiskelrc.json",
    ".aiskelrc",
    "config.toml",
    "settings.toml",
    "config.yaml",
    "config.yml",
    "settings.yaml",
    "settings.yml",
    "config.ini",
    "settings.ini",
)

DEFAULT_WRITE_PATHS_SAMPLE_CONTENT = """# aiskel 書き込み可能パス制御 設定サンプル (writable_paths.sample.txt)
#
# このファイルは参考用のサンプルです（このファイル自体は設定として読み込まれません）。
# 実際の設定は同じフォルダ内の config.json (または config.toml / config.yaml / *.txt) に記述してください。
#
# ==============================================================================
# 1. モード設定 (write_paths_mode)
# ==============================================================================
#   - "disabled"  : 書き込みパス制限を無効化（すべてのパスへ書き込み可。リストを残したまま一時停止可能）
#   - "whitelist" : ホワイトリストモード（allowed_write_paths に一致するパスのみ許可。空なら全拒否）
#   - "blacklist" : ブラックリストモード（denied_write_paths に一致するパスのみ拒否。それ以外は許可）
#   - "both"      : ホワイトリストとブラックリストを併用（許可対象のうち禁止対象を除外）
#   - "auto"      : 自動判定（デフォルト。非空のリストのみ有効化し、両方指定時はブラックリスト優先。両方空なら制限なし）
#
# ※ パス指定ルール:
#   - 相対パスはプロジェクトルート基準で解決されます
#   - ディレクトリ: "src/" または "src" -> 配下の全ファイル・サブディレクトリに一致
#   - 単一ファイル: "src/main.py" -> 指定ファイルのみに一致
#   - ワイルドカード(Glob): "*.env", "src/**/*.py", "config/*.secret.json" 等が利用可能
#
# ==============================================================================
# 2. config.json 記述例
# ==============================================================================
#
# [例A] ホワイトリスト（指定ディレクトリ・ファイルのみ書き込み許可）
# {
#   "write_paths_mode": "whitelist",
#   "allowed_write_paths": [
#     "src/",
#     "tests/",
#     "README.md"
#   ],
#   "denied_write_paths": []
# }
#
# [例B] ブラックリスト（重要ファイル・機密ファイルのみ書き込み禁止）
# {
#   "write_paths_mode": "blacklist",
#   "allowed_write_paths": [],
#   "denied_write_paths": [
#     ".env",
#     "*.secret.*",
#     "config/production.json",
#     "migrations/"
#   ]
# }
#
# [例C] ホワイトリスト + ブラックリスト併用（src/ 配下のみ許可しつつ、一部ファイルを除外）
# {
#   "write_paths_mode": "both",
#   "allowed_write_paths": [
#     "src/"
#   ],
#   "denied_write_paths": [
#     "src/generated/",
#     "src/core/secrets.py"
#   ]
# }
#
# [例D] 無効化（リスト設定を保持したまま、制限を一時的に無効化）
# {
#   "write_paths_mode": "disabled",
#   "allowed_write_paths": ["src/"],
#   "denied_write_paths": [".env"]
# }
#
# ==============================================================================
# 3. テキストファイル / YAML / TOML での記述例
# ==============================================================================
# [テキストファイル] 同フォルダに以下のファイルを配置（1行1パス、# でコメント）:
#   - ai_meta/allowed_write_paths.txt : ホワイトリスト用
#   - ai_meta/denied_write_paths.txt  : ブラックリスト用
#
# [YAML (config.yaml)]
#   write_paths_mode: whitelist
#   allowed_write_paths:
#     - src/
#     - tests/
#   denied_write_paths:
#     - "*.env"
#
# [TOML (config.toml)]
#   write_paths_mode = "blacklist"
#   denied_write_paths = [".env", "secrets/"]
"""
