import pathlib
import subprocess
import sys


child = pathlib.Path(__file__).with_name("subprocess_child.py")
result = subprocess.run([sys.executable, str(child)], check=True)
print("parent child returncode", result.returncode)
