"""ダブルクリックで gototask を起動してブラウザで開く（Windows は黒い窓を出さない）。

もう動いていれば、ブラウザで開くだけ。--no-browser を付けると開かずに裏で動く（PC 起動時用）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tasksmd as T  # noqa: E402

if sys.stdout is None or sys.stderr is None:  # pythonw・exe は出力先が無いので、ログに書く
    T.DATA.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(T.DATA / "serve.log", "a", encoding="utf-8", buffering=1)

import autostart  # noqa: E402
import serve  # noqa: E402

autostart.LAUNCHER = Path(__file__).resolve()  # このファイルの名前やフォルダを変えても、自動起動が追えるように
try:
    autostart.refresh()
except OSError:
    pass

serve.main(browser="--no-browser" not in sys.argv)
