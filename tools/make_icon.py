"""Write assets/hotwire.ico from the app's generated icon (used for the .exe)."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from hotwire.ui.common import make_icon  # noqa: E402

app = QApplication([])
out = Path(__file__).parents[1] / "assets" / "hotwire.ico"
out.parent.mkdir(exist_ok=True)
ok = make_icon().pixmap(64, 64).save(str(out), "ICO")
print(out, "OK" if ok else "FAILED")
