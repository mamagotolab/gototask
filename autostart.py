"""PC にログインしたら gototask の画面サーバーを裏で立ち上げる設定（Windows・Mac・Linux）。

Windows＝ログイン時に動くもの（レジストリの Run）、Mac＝LaunchAgents、Linux＝systemd のユーザー単位。
"""
import os
import subprocess
import sys
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "gototask"
LABEL = "com.mamagotolab.gototask"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PLAIN_NAME = "tasks"  # 設定でシンプル表示にしたときのレジストリの名前（gototask の名前を出さない）
LAUNCHER = None       # gototask.pyw から起動したら、その場所（ファイル名・フォルダ名を変えても自動起動が追える）


def run_name():
    import tasksmd as T
    return PLAIN_NAME if T.settings().get("icon") == "plain" else NAME


def command():
    """裏で起動するときのコマンド（ブラウザは開かない）。"""
    if getattr(sys, "frozen", False):  # exe・アプリ版
        return [sys.executable, "--no-browser"]
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")  # 黒い窓を出さない方の Python
        return [str(pyw if pyw.exists() else sys.executable), str(LAUNCHER or HERE / "gototask.pyw"), "--no-browser"]
    return [sys.executable, str(HERE / "serve.py")]


def _plist():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / f"{NAME}.service"


def _registered():
    """Windows：登録してあるコマンド（{名前: コマンド}）。"""
    import winreg
    out = {}
    for n in (NAME, PLAIN_NAME):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                out[n] = winreg.QueryValueEx(k, n)[0]
        except OSError:
            pass
    return out


def enabled():
    if sys.platform == "win32":
        return bool(_registered())
    if sys.platform == "darwin":
        return _plist().exists()
    return _unit().exists()


def enable():
    """登録する。すでに登録してあれば触らない（自分で書いた設定を上書きしない）。"""
    if enabled():
        return
    cmd = command()
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, run_name(), 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
    elif sys.platform == "darwin":
        args = "".join(f"<string>{escape(a)}</string>" for a in cmd)
        _plist().parent.mkdir(parents=True, exist_ok=True)
        _plist().write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{LABEL}</string>'
            f'<key>ProgramArguments</key><array>{args}</array><key>RunAtLoad</key><true/></dict></plist>\n',
            encoding="utf-8")
    else:
        _unit().parent.mkdir(parents=True, exist_ok=True)
        _unit().write_text(
            f"[Unit]\nDescription=gototask (local task viewer)\n\n[Service]\nExecStart={subprocess.list2cmdline(cmd)}\n"
            "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "enable", f"{NAME}.service"], capture_output=True)


def disable():
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                for n in _registered():
                    winreg.DeleteValue(k, n)
        except OSError:
            pass
    elif sys.platform == "darwin":
        _plist().unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", f"{NAME}.service"], capture_output=True)
        if not _unit().is_symlink():  # 自分でリンクを張った設定は消さない
            _unit().unlink(missing_ok=True)


def refresh():
    """Windows：登録済みなら、今の名前・場所に合わせて登録し直す（表示の設定を変えた・フォルダやファイルの名前を変えたとき）。"""
    if sys.platform != "win32":
        return
    reg = _registered()
    if reg and reg != {run_name(): subprocess.list2cmdline(command())}:
        disable()
        enable()
