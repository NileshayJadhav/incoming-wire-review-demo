# Optional read-only SharePoint staging for Langflow

Implemented locally; tenant access has not been tested. Importing the module makes no network requests. The adapter stages an immutable local batch snapshot and manifest; it does not review files or upload results.

1. Use a company-approved existing delegated Microsoft Graph authentication route. Have that route supply its short-lived token to the process environment as `IWT_GRAPH_TOKEN`; do not type the token into chat, save it in source code, or pass it as a command-line argument. This package does not register an app, request consent, acquire a token, refresh a token or change permissions.
2. Prefer an existing document-library `drive_id` and folder `folder_id`. The token must already have read access accepted by those endpoints. Microsoft documents delegated `Files.Read` for file listing/download; cross-site discovery may require existing `Sites.Read.All`. Have your administrator determine the allowed least-privilege configuration. The adapter never asks for broader scopes. [List children](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0), [download content](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0).
3. Run from the project directory, using your local Python runtime:

```powershell
python -m reusable_lab.cloud.sharepoint_adapter --tenant-host contoso.sharepoint.com --drive-id EXISTING_DRIVE_ID --folder-id EXISTING_FOLDER_ID --destination .\private_staging
```

Alternatively, supply `--folder-url "https://contoso.sharepoint.com/sites/Audit/Shared%20Documents/Wires/Batch01"` in place of the drive/folder IDs. Canonical folder URLs, library `AllItems.aspx?id=...` URLs and `/:f:/r/sites/...` URLs are normalized without visiting the supplied URL. Only commercial `tenant.sharepoint.com` hosts explicitly listed with `--tenant-host` are supported. Government/national clouds, OneDrive personal sites, root-site paths, external shortcuts and opaque sharing links need a separately approved adapter configuration.

4. Read the returned status. **Only `COMPLETE` is eligible for review.** The manifest contains safe relative paths, local paths, original file sizes, source etags/version metadata when available and SHA-256 for every staged file. A failed, changed, oversized or invalid supported file leaves the snapshot `INCOMPLETE`; do not bypass this gate.
5. Select the staged `.xlsx` path and the staged documents subfolder in your normal Langflow review request. Keep the manifest with the resulting draft evidence report. Review happens on the staged copies; original cloud files remain untouched. The source snapshot is not automatically refreshed, so run staging again for a new or modified batch.

The adapter recursively follows child pages; limits are 250 supported files, 30 MiB per file, 250 MiB combined, 20 folder levels and bounded page/item counts. Only PDF/XLSX are staged. Unsupported extensions are listed under `skipped`. Missing required evidence must remain OPEN during review. Preauthenticated download redirects are allowed only to the exact approved SharePoint host, and receive no Graph bearer token. No download URLs or tokens are persisted. Byte counts, PDF/Excel container headers and source metadata after download are checked. Corporate proxy inheritance is intentionally disabled; an approved networking configuration may be needed on some company devices.

Opaque `/:f:/s/...` sharing links are rejected. The current Shares API documents write scopes and optional link-redemption headers, so this read-only adapter deliberately does not call that endpoint, request write scopes or redeem a link. Ask for the canonical folder location or existing drive/folder IDs. [Microsoft: shared-item API permissions and redemption behavior](https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0).

Staging folders and manifests may contain confidential data and tenant identifiers. Keep them outside any public GitHub package. Only fictional offline tests belong in a public demo.

Python API:

```python
from reusable_lab.cloud.sharepoint_adapter import stage_sharepoint_folder

manifest = stage_sharepoint_folder(
    destination_root="private_staging",
    allowed_tenant_hosts=["contoso.sharepoint.com"],
    drive_id="EXISTING_DRIVE_ID",
    folder_id="EXISTING_FOLDER_ID",
)
if manifest["status"] != "COMPLETE":
    raise ValueError("SharePoint snapshot incomplete; do not start the review")
# Pass only explicit selected local paths from manifest["files"] to the review.
```
