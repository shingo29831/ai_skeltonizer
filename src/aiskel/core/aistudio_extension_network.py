"""Module: @role: ローカルブリッジ接続先のホスト正規化・URL整形・ループバック判定および出力先・認証トークン管理を担当する。"""
import ipaddress
import os
import secrets
from pathlib import Path
from typing import Optional

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18765
EXTENSION_DIR_NAME = "aistudio_bridge"
EXTENSION_VERSION = "1.3.0"
FIREFOX_DIR_SUFFIX = "_firefox"
TOKEN_FILENAME = ".bridge_token"

def _parse_ip_without_zone(raw_host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """IPリテラルを解析し、ブラウザURLで非対応のIPv6ゾーンID(%...)を拒否する。"""
    try:
        addr = ipaddress.ip_address(raw_host)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and getattr(addr, "scope_id", None):
        raise ValueError(f"IPv6ゾーンID付きホストはブラウザ連携URLでサポートされていません: {raw_host}")
    return addr

def normalize_bind_host(host: str) -> str:
    """ソケットbind用にIPv6角括弧を除去し、IPリテラルの表記ゆれを正規化する。"""
    normalized = (host or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("[") and normalized.endswith("]"):
        inner = normalized[1:-1].strip()
        addr = _parse_ip_without_zone(inner)
        if isinstance(addr, ipaddress.IPv6Address):
            return addr.compressed
        return normalized
    addr = _parse_ip_without_zone(normalized)
    if addr is not None:
        return addr.compressed
    return normalized

def format_host_for_url(host: str) -> str:
    """URLホスト部用にIPv6リテラルをRFC 3986準拠の角括弧付きへ整形する。"""
    unwrapped = normalize_bind_host(host)
    if not unwrapped:
        return unwrapped
    addr = _parse_ip_without_zone(unwrapped)
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{addr.compressed}]"
    return unwrapped

def resolve_client_host(bind_host: str) -> str:
    """ワイルドカード待受ホストをブラウザ接続可能なループバックIPへ変換する。"""
    normalized = normalize_bind_host(bind_host)
    if not normalized:
        return "127.0.0.1"
    addr = _parse_ip_without_zone(normalized)
    if addr is not None:
        if addr.is_unspecified:
            # IPV6_V6ONLY環境でのlocalhost→127.0.0.1解決失敗を防ぐためIPv6は::1へ変換
            return "::1" if isinstance(addr, ipaddress.IPv6Address) else "127.0.0.1"
        return addr.compressed
    return normalized

def is_loopback_host(host: str) -> bool:
    """指定ホストがIPv4/IPv6の表記ゆれを含めループバックに該当するか判定する。"""
    normalized = normalize_bind_host(host)
    if normalized.lower() == "localhost":
        return True
    addr = _parse_ip_without_zone(normalized)
    return bool(addr is not None and addr.is_loopback)

def get_default_extension_dir(
    port: int = DEFAULT_BRIDGE_PORT,
    home_dir: Optional[Path] = None,
) -> Path:
    """ポート競合を避ける拡張機能ファイルのデフォルト出力先パスを返す。"""
    base = home_dir if home_dir is not None else Path.home()
    dir_name = EXTENSION_DIR_NAME if port == DEFAULT_BRIDGE_PORT else f"{EXTENSION_DIR_NAME}_{port}"
    return base / ".aiskel" / "extensions" / dir_name

def get_firefox_extension_dir(extension_dir: Path) -> Path:
    """Chrome/Edge用拡張ディレクトリに対応するFirefox用拡張ディレクトリパスを返す。"""
    resolved = extension_dir.resolve()
    if not resolved.name:
        return resolved / "firefox"
    return resolved.parent / f"{resolved.name}{FIREFOX_DIR_SUFFIX}"

def _write_token_file(token_path: Path, token_value: str) -> None:
    content = token_value + "\n"
    if token_path.is_file():
        try:
            if token_path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    token_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass

def load_or_create_bridge_token(
    extension_dir: Path,
    explicit_token: Optional[str] = None,
) -> str:
    """明示トークンまたは拡張ディレクトリに永続化されたランダム認証トークンを返す。"""
    resolved_dir = extension_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    token_path = resolved_dir / TOKEN_FILENAME

    # 次回--token省略起動時の不整合を防ぐため明示トークンも永続化
    if explicit_token is not None and explicit_token.strip():
        cleaned = explicit_token.strip()
        _write_token_file(token_path, cleaned)
        return cleaned

    if token_path.is_file():
        try:
            existing = token_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass

    generated = secrets.token_urlsafe(24)
    _write_token_file(token_path, generated)
    return generated
