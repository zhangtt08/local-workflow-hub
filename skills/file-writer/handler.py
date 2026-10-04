import json
import os
import sys
from pathlib import Path

data = json.load(sys.stdin)
root = Path(os.environ["WORKFLOWHUB_ARTIFACT_ROOT"]).resolve()
filename = data["filename"]
if Path(filename).name != filename or filename in {".", ".."}:
    json.dump({"error": {"code": "input_error", "message": "filename must be a plain name"}}, sys.stdout)
    sys.exit(1)
path = (root / filename).resolve()
if not path.is_relative_to(root):
    json.dump({"error": {"code": "permission_error", "message": "path escapes artifact root"}}, sys.stdout)
    sys.exit(1)
path.write_text(data["text"], encoding="utf-8")
json.dump({"outputs": {"file_path": str(path)}, "artifacts": [{"uri": str(path), "media_type": "text/plain"}]}, sys.stdout, ensure_ascii=False)
