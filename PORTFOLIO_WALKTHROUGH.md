# Demonstrate the agent in five minutes

1. Explain the task: a reviewer receives a workbook and PDFs, then needs evidence-linked draft checks written back into the same layout.
2. Show the six Langflow nodes. Chat Input passes the request to Agent. Inventory discovers the layout and files. Review starts a bounded background job. Job Control provides status/cancel/resume. Chat Output presents the draft links.
3. Show the fictional source XLSX and one incoming PDF. Give Playground their absolute paths, the evidence-folder path, sample IDs `ALPHA-17,OMEGA-3` and the absolute `fictional_2026_rules.json` path. State whether you are using live OpenAI extraction or the offline deterministic demo.
4. Show the job ID and request `status JOBID`. Open the separate output workbook and `Review_Report.json`. Trace one X to the source file/page/quote, then show an OPEN check with missing evidence. Original files remain unchanged.
5. Run `python -m reusable_lab.evaluation --offline --output evaluation-results.json`. Explain expected-versus-actual labels, source-quote validation and false-supported checks. An offline regression result is not a claim of production model accuracy.

Use this factual project description: “I configured and tested a local Langflow incoming-wire review prototype with page-cited extraction, deterministic reviewer criteria, a preserving Excel writer, background jobs and evaluation tooling. It produces drafts for human review.”

The project is a local prototype. Copilot Studio implementation is documented separately. Company deployment, independent policy verification and production operating effectiveness have not been established by this repository.
