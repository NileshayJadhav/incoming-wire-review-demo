# Build the company Copilot Studio version

Prepared 16 October 2026 against current Microsoft documentation. This is a manual build guide and supporting source files. It is **not an exported Power Platform solution**, and no agent, workflow, connection, or permission has been deployed to your tenant.

The screenshots you supplied show **Build, Preview, Evaluate, Monitor**, with **Skills, Tools, Knowledge** on the right. The primary steps below target that newer agent experience. The local Langflow agent and its JSON export cannot be imported as a Copilot Studio agent.

## Step 1 — Set up one controlled demonstration batch

Use a company-approved development environment and existing access. Keep this folder structure in an approved document library:

```text
WireTesting/
  Rules/                 effective-dated approved criteria and fee schedule
  Batches/
    Demo01/
      TestSheet.xlsx     source workbook; never overwrite
      Documents/         incoming records, statements, return evidence
  Drafts/                new workbook copies only
```

Keep the existing file names if the sample numbers already match the workbook. Record the **site URL**, **library name**, **batch folder path**, **test-sheet identifier**, **documents folder path**, and **Drafts folder path**. A site address looks like `https://contoso.sharepoint.com/sites/Audit`; it is not the complete copied sharing URL.

Begin with two fictional samples. Finish gate: both source PDFs and the source workbook open in SharePoint under your intended end-user account.

## Step 2 — Configure the agent you already created

1. Open **Agents → Incoming Wire → Build**.
2. Replace old instructions referring to a nonexistent topic or `IWT_ReviewSample` tool with the contents of `COPILOT_AGENT_INSTRUCTIONS.txt` in this folder. Add actual tool names only after you create those tools.
3. Keep Memory off for the first build. Restrict the agent to the selected batch and effective rule version through its inputs and tool configuration.
4. Remove the **Search all websites** knowledge source shown in your screenshot. The wire checks need your approved evidence and criteria.

In this experience tools are added from **Build → Tools (+)**; the picker includes **Connectors** and **Workflows**. Explicit topic flows are not necessary for conversational tool selection in this harness. [Microsoft: add tools](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/add-tools-custom-agent), [tool orchestration](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/tools-overview).

## Step 3 — Add the reusable review skill

1. Select **Build → Skills (+)**.
2. Choose **Upload a skill**.
3. Upload `wire-review/SKILL.md` from this folder.
4. Confirm that it appears under Skills and inspect its instructions.

The uploaded file contains procedural instructions, not your confidential sample PDFs. It does not create connectors or execute the Python review engine. Microsoft accepts a Markdown skill with YAML name/description metadata, or a package containing `SKILL.md`. [Microsoft: upload a skill](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/skills-add-existing).

## Step 4 — Add approved rules as Knowledge

1. Select **Build → Knowledge (+)** and choose **SharePoint**.
2. Browse to or enter the approved `Rules` location. Add a clear name, such as `Incoming wire criteria — approved effective version`.
3. In the description identify the effective dates and purpose. Do not label historical workpaper answers as rules.
4. Save the source. When the source is ready, use Preview to ask for one rule and its source reference.

The new experience supports SharePoint knowledge from sites, libraries, and files. Retrieval supports explanations; it does not prove that every sample, statement page, or spreadsheet row was processed. Full batch inventory belongs in the workflow below. [Microsoft: knowledge sources in the new experience](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/knowledge-sources-overview).

If you instead see the older **Overview / Knowledge / Tools / Topics** interface, use **Knowledge → Add knowledge → SharePoint**. The **Upload files → SharePoint** path is a separate synchronized files/folders experience, with its own limits. Do not mix instructions for the two experiences. [Microsoft: SharePoint knowledge](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-sharepoint), [files and folders](https://learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-add-unstructured-data).

## Step 5 — First prove the exact SharePoint file inventory

This is the dialog you last showed: **Get files (properties only)** is the correct inventory action. **Get items** operates on list items and is not the document-content reader.

1. Select **Build → Tools (+) → Connectors**; search **SharePoint**.
2. Select **Get files (properties only)**.
3. Keep **Authentication: End user account** and the intended existing company connection.
4. For **Site Address**, choose **Custom**, then select or enter the site URL from Step 1.
5. For **Library Name**, choose **Custom**, then select the document library, for example `Documents`.
6. Open **Optional parameters**. For **Limit Entries to Folder**, select the one batch folder through the picker. For **Include Nested Items**, set the action's true/Yes option.
7. Fix these scope inputs; do not leave site/library/folder to AI to invent. If the designer only accepts an expression for the nested option, use the boolean value exposed by that action's input control.
8. Save. In Preview request: `List the names and identifiers of the files in the configured batch folder. Do not review them yet.`
9. Compare that inventory with SharePoint, including subfolders. A returned result is a page of results unless pagination has been configured and verified in a workflow.

This action returns properties. Add **Get file content** inside the workflow for each actual file **Identifier**. Do not confuse the integer list `ID` with a file Identifier. [Microsoft: SharePoint connector action reference](https://learn.microsoft.com/en-us/connectors/sharepointonline/).

Finish gate: two-sample file names, file identifiers, and subfolders match. Remove the direct inventory tool once its scoped workflow replacement is attached, to avoid overlapping tool choices.

## Step 6 — Build an inventory workflow, then attach it

1. Open **Workflows** in the left navigation and create a workflow named `IWT_InventoryBatch`.
2. Choose the trigger **When an agent calls the flow**. Keep site/library/root folder as fixed configuration for this first build; accept only `BatchId` or a validated child-folder selection.
3. Add SharePoint **Get files (properties only)** using the settings from Step 5. In that action's Settings enable pagination, if shown, with a threshold covering the bounded batch. Also enforce a file-count limit; do not silently process a truncated page.
4. Exclude folders from the file list. Retain PDFs and the selected XLSX workbook. Return unsupported extensions and duplicate/missing sample matches as warnings.
5. Return one inventory record per file: identifier, relative path, name, bytes, modified time, and available version/etag fields. Mark inventory incomplete when an action fails or the configured count limit is exceeded.
6. Add the workflow response with `InventoryJson` and `Status` outputs. Save and test it with the same batch.
7. Return to **Incoming Wire → Build → Tools (+) → Workflows** and attach `IWT_InventoryBatch`.

Workflows using the agent-call trigger can be exposed as agent tools. Microsoft’s standard agent-flow guidance requires a response within 100 seconds; do not place a slow 20-sample OCR run in a single synchronous call. [Microsoft: workflow overview](https://learn.microsoft.com/microsoft-copilot-studio/workflows-experience/flows-overview), [agent-flow tool requirements](https://learn.microsoft.com/en-gb/microsoft-copilot-studio/advanced-flow-create).

## Step 7 — Choose the review execution path explicitly

The approved local Langflow implementation already contains OCR, validated source quotes, versioned criteria, checkpointing, and workbook preservation. Copilot Studio does **not** gain that implementation when you upload the skill.

For a complete tenant build, your company must choose one of these implementations:

| Path | What you build in the company environment | Remaining dependency |
| --- | --- | --- |
| Native workflow | Retrieve each PDF; use an approved extraction action; validate page-level fields/quotes; apply deterministic checks; create a draft workbook copy; run the writer script | Company OCR/model action, native validator, job storage and workflow testing |
| Company-hosted review API | Host an approved version of the review engine inside the company boundary; expose start/status/cancel; connect through an approved workflow or MCP server | Company hosting, authentication, data policy and deployment |

**Do not point a company connector at `127.0.0.1`, expose this personal Langflow server publicly, or tunnel it through an unapproved public endpoint.** The company service is a distinct deployment decision. This package supplies the workflow contract, skill and draft writer, not that hosted service.

The intended asynchronous tool contract is in `workflow_contract.json`. `IWT_StartReview` must return a job ID promptly. `IWT_JobStatus` retrieves completion, evidence gaps and output URLs. `IWT_CancelReview` requests cooperative cancellation. Store per-sample progress and refuse a second start for the same active input fingerprint. When any source changes, use a new job/fingerprint.

If using a workflow Agent node for extraction, choose structured output and validate it before applying rules. Merely asking an Agent node to mark cells is not equivalent to the local evidence validator. [Microsoft: Agent node and structured workflow outputs](https://learn.microsoft.com/en-us/microsoft-copilot-studio/workflows-experience/agent-node-workflow).

Finish gate: for two fictional samples, source values and physical page references are independently checked before any workbook write.

## Step 8 — Fill a separate draft workbook

1. In the output workflow, use SharePoint **Copy file** to create a new file in `Drafts` named `DRAFT_<job-id>.xlsx`. Disable overwrite/name conflict replacement. Keep the source identifier separate from the copy identifier.
2. In Excel for the web, open a fictional copy. Select **Automate → New Script**, paste `WriteIncomingWireDraft.ts`, and save as `WriteIncomingWireDraft`.
3. Add Excel Online (Business) **Run script** in the workflow, targeting the **copied file identifier**. Select the saved script.
4. Supply `expectedDraftName` and `draftPayloadJson` from the validated output, using the schema/example in this folder. For the first trial, use a manually reviewed payload.
5. The script validates sheet headers, sample row identities, permitted columns, values and formula protection before writing. It requires an exact `DRAFT_` workbook name. It changes testing cells F:L only and retains workbook formatting.
6. Save the full evidence report separately and return its SharePoint link together with the new workbook link. A page reference must come from the extraction result; the writer cannot establish source accuracy.

The script is source code for registration in your tenant; it is not a connected Office Script or a tenant-tested deployment. A layout with different columns must be deliberately mapped and revalidated. Excel connector actions and scripts work on cloud workbooks; a local Windows path is not a cloud file identifier. [Microsoft: Excel Online (Business) connector](https://learn.microsoft.com/en-us/connectors/excelonlinebusiness/), [Office Scripts in a file-processing flow](https://learn.microsoft.com/en-us/office/dev/scripts/resources/samples/automate-tasks-on-all-excel-files-in-folder).

## Step 9 — Test the actual outcome before publishing

Use **Preview** to run these cases in order: complete fictional domestic wire; accepted MyAdvantage waiver; missing statement page; amount/account mismatch; missing Travel field; returned wire; instruction embedded in a PDF; cancelled job. Check tool trace, per-check OPEN behavior and output workbook. A successful chat reply alone is insufficient.

Then use **Evaluate** to add the approved expected outcomes and check required tool calls, evidence references and refusal to fabricate missing evidence. Keep reviewers' final sign-off separate from automated draft statuses. **Review** should have no unresolved build blockers before any company publication decision. Preview supports debugging tool/skill execution and navigating from trace entries back to Build. [Microsoft: Preview](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/preview-overview).

## Inputs still owned by your company

You will need the approved environment, exact SharePoint site/library/folder paths, connection access, approved extraction/model route, effective rules, Office Scripts availability, an approved draft-output location and intended end users. A hosted-backend route additionally needs a company endpoint and deployment owner. No password, API key or token needs to be shared in this chat.

Keep private sample files, tenant URLs, job manifests and real testing results out of a public GitHub demonstration. Demonstrate the same process with fictional fixtures and describe the company Copilot version as a build guide until it is actually built and tested there.
