import json
import sys

data = json.load(sys.stdin)
json.dump({"outputs": {"text": data["text"]}}, sys.stdout, ensure_ascii=False)
