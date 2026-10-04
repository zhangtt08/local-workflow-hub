"""Run the deterministic example without an LLM or web server."""

import argparse
import asyncio
import json

from .bootstrap import PROJECT_ROOT, build_hub
from .models import WorkflowDefinition


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="hello workflowhub")
    args = parser.parse_args()
    hub = build_hub()
    workflow = WorkflowDefinition.model_validate_json((PROJECT_ROOT / "examples" / "text_to_file.json").read_text(encoding="utf-8"))
    hub.workflows.save_version(workflow)
    run = await hub.runner.run(workflow, {"text": args.input})
    print(json.dumps({
        "run_id": run.id, "trace_id": run.trace_id, "workflow_version_id": run.workflow_version_id,
        "status": run.status, "outputs": run.outputs,
        "error": run.error.model_dump(mode="json") if run.error else None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
