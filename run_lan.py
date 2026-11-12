from pathlib import Path
import os
import runpy

root = Path(__file__).resolve().parent
os.chdir(root)
os.environ["IDC_HOST"] = "0.0.0.0"
os.environ["IDC_PORT"] = "8002"
runpy.run_path(str(root / "app.py"), run_name="__main__")
