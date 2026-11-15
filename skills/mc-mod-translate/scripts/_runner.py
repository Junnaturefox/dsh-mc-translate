import subprocess
import sys

out_path, err_path, exit_path = sys.argv[1], sys.argv[2], sys.argv[3]
cmd = sys.argv[4:]
with open(out_path, "wb") as o, open(err_path, "wb") as e:
    rc = subprocess.call(cmd, stdout=o, stderr=e)
with open(exit_path, "w", encoding="utf-8") as f:
    f.write(str(rc))
sys.exit(0)
