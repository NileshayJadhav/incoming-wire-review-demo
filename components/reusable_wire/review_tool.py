import sys
import os
from importlib import reload

from langflow.custom import Component
from langflow.io import MessageTextInput, Output, SecretStrInput, StrInput, BoolInput, IntInput
from langflow.schema import Data


class ReusableWireReview(Component):
    display_name = "Reusable Wire Review"
    description = "Start one background wire review and return its job ID immediately. Read current local wire PDFs, validate page-cited facts and fill a separate draft XLSX. Check progress with Reusable Wire Job; cancel or resume with the exact job ID. Missing/ambiguous evidence stays OPEN. Exact user-supplied local paths only."
    icon = "FileCheck2"
    name = "ReusableWireReview"
    inputs = [
        MessageTextInput(name="workbook_path", display_name="Test-sheet path", value="", required=True, tool_mode=True),
        MessageTextInput(name="documents_folder", display_name="Documents folder", value="", required=True, tool_mode=True),
        MessageTextInput(name="sample_request", display_name="Samples", value="", required=True, info="all or exact IDs separated by commas. Max20 samples per run.", tool_mode=True),
        MessageTextInput(name="rules_path", display_name="Rules JSON path", value="", required=True, info="Use the explicit fictional rules JSON included with this demo. Never apply demo rules to real records.", tool_mode=True),
        MessageTextInput(name="mapping_path", display_name="Mapping JSON path", value="", required=True, info="Optional explicit workbook layout and document mapping.", tool_mode=True),
        SecretStrInput(name="api_key", display_name="OpenAI API Key", value="", required=False, info="Select the existing saved Open AI Key variable. Never paste a key in chat."),
        StrInput(name="model_name", display_name="Extraction model", value="gpt-4o-mini", advanced=True),
        BoolInput(name="force_refresh", display_name="Refresh extracted facts", value=False, advanced=True,
                  info="Read current PDFs every time. Enable this to bypass unchanged-document model fact cache."),
        IntInput(name="workers", display_name="Extraction workers", value=2, advanced=True,
                 info="1 or 2. Two bounded model requests at a time; workbook order stays unchanged."),
    ]
    outputs = [Output(name="review", display_name="Review", method="review_wire_batch")]

    def review_wire_batch(self) -> Data:
        root = os.environ.get("IWT_PROJECT_ROOT") or os.getcwd()
        if root not in sys.path:
            sys.path.insert(0, root)
        from reusable_lab import jobs
        with jobs._LOCK:
            reload(jobs)
        try:
            from langchain_openai import ChatOpenAI
            key = self.api_key.get_secret_value() if hasattr(self.api_key, "get_secret_value") else self.api_key
            if not key:
                return Data(data={"ok": False, "status": "BLOCKED", "error": "Select the existing OpenAI key in this component."})
            model = ChatOpenAI(model=self.model_name or "gpt-4o-mini", api_key=key,
                               temperature=0, max_retries=1, timeout=120, max_tokens=12000)
            result = jobs.start_review(self.workbook_path, self.documents_folder, self.sample_request or "all",
                self.rules_path or "", self.mapping_path or "", model=model,
                model_name=self.model_name or "gpt-4o-mini", force_refresh=bool(self.force_refresh), workers=int(self.workers))
        except Exception as exc:
            result = {"ok": False, "status": "BLOCKED", "error_type": type(exc).__name__,
                      "error": "Review setup failed; no completed draft is claimed. Inspect local configuration."}
        self.status = {"ok": result.get("ok"), "status": result.get("status")}
        return Data(data=result)
