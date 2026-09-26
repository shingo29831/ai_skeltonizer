import sys
import subprocess
import platform

def get_clipboard_text() -> str:
    """OSのクリップボードからテキストを取得する"""
    system = platform.system()
    try:
        if system == "Darwin":
            text = subprocess.check_output(["pbpaste"], encoding="utf-8")
        elif system == "Windows":
            text = subprocess.check_output(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw"
                ],
                encoding="utf-8"
            )
        elif system == "Linux":
            try:
                text = subprocess.check_output(["wl-paste", "--no-newline"], encoding="utf-8")
            except (FileNotFoundError, subprocess.CalledProcessError):
                try:
                    text = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], encoding="utf-8")
                except FileNotFoundError:
                    text = subprocess.check_output(["xsel", "--clipboard", "--output"], encoding="utf-8")
        else:
            print(f"⚠️ OS {system} のクリップボード自動取得は未対応です。", file=sys.stderr)
            return ""
            
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except Exception as e:
        print(f"⚠️ クリップボードの読み込みに失敗しました: {e}", file=sys.stderr)
        return ""

def set_clipboard_text(text: str) -> None:
    """OSのクリップボードにテキストを書き込む"""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["pbcopy"], input=text, encoding="utf-8", check=True)
    elif system == "Windows":
        try:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; $input | Set-Clipboard"
                ],
                input=text,
                encoding="utf-8",
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            subprocess.run(["clip.exe"], input=text, encoding="utf-8", check=True)
    elif system == "Linux":
        success = False
        last_error = None
        for cmd in [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"]
        ]:
            try:
                subprocess.run(cmd, input=text, encoding="utf-8", check=True)
                success = True
                break
            except (FileNotFoundError, subprocess.CalledProcessError) as err:
                last_error = err
                continue
        if not success:
            raise RuntimeError(f"Linuxのクリップボードユーティリティ(wl-copy, xclip, xsel)が見つからないか実行に失敗しました: {last_error}")
    else:
        raise NotImplementedError(f"OS {system} のクリップボード書き込みは未対応です。")
