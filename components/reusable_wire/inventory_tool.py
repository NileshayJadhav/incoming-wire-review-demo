import sys
import os
from importlib import reload
from pathlib import Path

from langflow.custom import Component
from langflow.io import MessageTextInput, Output
from langflow.schema import Data


class ReusableWireInventory(Component):
    display_name = "Reusable Wire Inventory"
    description = "Inventory an explicitly supplied local XLSX test sheet and recursive PDF evidence folder. Discover sample rows and columns; match PDFs by exact sample IDs or an optional mapping JSON. No model calls or source edits. Never invent a path."
    icon = "FolderSearch"
    name = "ReusableWireInventory"
    inputs = [
        MessageTextInput(name="workbook_path", display_name="Test-sheet path", value="", required=True, tool_mode=True),
        MessageTextInput(name="documents_folder", display_name="Documents folder", value="", required=True, tool_mode=True),
        MessageTextInput(name="sample_request", display_name="Samples", value="", required=True, info="all, or exact sample IDs separated by commas (max20 per run).", tool_mode=True),
        MessageTextInput(name="rules_path", display_name="Rules JSON path", value="", required=True, info="Blank uses the confirmed Aug-Dec2025 reviewer criteria.", tool_mode=True),
        MessageTextInput(name="mapping_path", display_name="Mapping JSON path", value="", required=True, info="Optional explicit worksheet columns and document pairing.", tool_mode=True),
    ]
    outputs = [Output(name="inventory", display_name="Inventory", method="inventory_wire_batch")]

    def inventory_wire_batch(self) -> Data:
        root = os.environ.get("IWT_PROJECT_ROOT") or os.getcwd()
        if root not in sys.path:
            sys.path.insert(0, root)
        from reusable_lab import engine
        result = reload(engine).inventory_batch(self.workbook_path, self.documents_folder,
            self.sample_request or "all", self.rules_path or "", self.mapping_path or "")
        self.status = {"ok": result.get("ok"), "status": result.get("status")}
        return Data(data=result)
