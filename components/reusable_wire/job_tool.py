import sys
import os
from importlib import reload

from langflow.custom import Component
from langflow.io import MessageTextInput, Output, SecretStrInput, StrInput
from langflow.schema import Data


class ReusableWireJob(Component):
    display_name = "Reusable Wire Job"
    description = "Check status, cancel, or resume a local background wire review using its exact returned job ID. Status returns progress and completed draft links. Cancel is cooperative: a current model/OCR call may finish before stopping. Resume uses saved validated sample checkpoints only when the original sources/rules/validators match."
    icon = "ListChecks"
    name = "ReusableWireJob"
    inputs = [
        MessageTextInput(name="job_id", display_name="Job ID", value="", required=True, tool_mode=True),
        MessageTextInput(name="action", display_name="Action", value="status", required=True,
                         info="status, cancel or resume", tool_mode=True),
        SecretStrInput(name="api_key", display_name="OpenAI API Key", value="", required=False,
                       info="Select the existing saved Open AI Key alias for resume; status/cancel need no model call."),
        StrInput(name="model_name", display_name="Extraction model", value="gpt-4o-mini", advanced=True),
    ]
    outputs = [Output(name="job", display_name="Job", method="manage_wire_job")]

    def manage_wire_job(self) -> Data:
        root = os.environ.get("IWT_PROJECT_ROOT") or os.getcwd()
        if root not in sys.path:
            sys.path.insert(0, root)
        from reusable_lab import jobs
        with jobs._LOCK:
            reload(jobs)
        try:
            action = (self.action or "status").strip().casefold()
            model = None
            if action == "resume":
                from langchain_openai import ChatOpenAI
                key = self.api_key.get_secret_value() if hasattr(self.api_key, "get_secret_value") else self.api_key
                if not key:
                    return Data(data={"ok": False, "status": "BLOCKED", "error": "Select the existing saved OpenAI key to resume."})
                # Use the job's saved model name, never silently migrate its extraction model.
                state = jobs.control(self.job_id, "status")
                if not state.get("job_id"):
                    return Data(data=state)
                saved_model = jobs._read(self.job_id)["request"]["model_name"]
                model = ChatOpenAI(model=saved_model, api_key=key, temperature=0,
                                   max_retries=1, timeout=120, max_tokens=12000)
            result = jobs.control(self.job_id, action, model=model)
        except Exception:
            result = {"ok": False, "status": "BLOCKED", "error": "Job setup failed; inspect the local job ID and configuration."}
        self.status = {"status": result.get("status"), "job_id": result.get("job_id")}
        return Data(data=result)
