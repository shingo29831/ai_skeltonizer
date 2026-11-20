"""Module: @role: macOS環境におけるデフォルトブラウザのBundle ID解決および最上位表示制御を担当する。"""
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Optional

_DEFAULT_SAFARI_BUNDLE_ID = "com.apple.Safari"
_VALID_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+$")
_LSAPPINFO_BUNDLE_RE = re.compile(r'bundleID=(?:"([^"]+)"|([^\s]+))')


def _get_macos_default_browser_bundle_id() -> str:
    """macOSのLaunchServices設定からデフォルトブラウザのBundle IDを取得する。"""
    plist_path = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    if plist_path.exists():
        try:
            with plist_path.open("rb") as f:
                data = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError, EOFError):
            data = None

        if isinstance(data, dict):
            handlers = data.get("LSHandlers", [])
            if isinstance(handlers, list):
                for scheme in ("https", "http"):
                    for item in handlers:
                        if not isinstance(item, dict) or item.get("LSHandlerURLScheme") != scheme:
                            continue
                        for role_key in ("LSHandlerRoleAll", "LSHandlerRoleViewer"):
                            val = item.get(role_key)
                            if isinstance(val, str):
                                candidate = val.strip()
                                if _VALID_BUNDLE_ID_RE.fullmatch(candidate):
                                    return candidate
    return _DEFAULT_SAFARI_BUNDLE_ID


def _resolve_running_macos_bundle_id(target_bundle_id: str) -> Optional[str]:
    """lsappinfo listから大文字小文字を無視して起動中アプリの正規Bundle IDを解決する。"""
    res = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=2)
    if res.returncode != 0:
        return None

    target_lower = target_bundle_id.lower()
    for match in _LSAPPINFO_BUNDLE_RE.finditer(res.stdout):
        running_id = (match.group(1) or match.group(2) or "").strip()
        if running_id.lower() == target_lower and _VALID_BUNDLE_ID_RE.fullmatch(running_id):
            return running_id
    return None


def focus_macos_default_browser_target() -> Optional[str]:
    """起動済みのmacOSデフォルトブラウザをアクティブ化し、成功時はBundle IDを返す。"""
    try:
        bundle_id = _get_macos_default_browser_bundle_id()
        if not _VALID_BUNDLE_ID_RE.fullmatch(bundle_id):
            return None

        # plist内の小文字表記を吸収しつつ未インストール時の検索ダイアログを抑止する
        resolved_id = _resolve_running_macos_bundle_id(bundle_id)
        if not resolved_id:
            return None

        # Apple Events(TCC Automation)権限拒否を回避するため open -b で前面化する
        res = subprocess.run(["open", "-b", resolved_id], capture_output=True, text=True, timeout=3)
        return resolved_id if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def focus_macos_default_browser() -> bool:
    """起動済みのmacOSデフォルトブラウザのみをアクティブ化する。"""
    return focus_macos_default_browser_target() is not None
