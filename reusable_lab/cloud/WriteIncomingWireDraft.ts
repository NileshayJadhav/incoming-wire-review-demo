/** Register manually in Excel for the web. Writes only a named DRAFT copy.
 * This is a guarded writer; it does not review PDFs or validate citations.
 * The flow must pass a reviewer-approved or independently validated payload.
 */
interface DraftSample {
  row: number;
  sampleId: string;
  customer: string;
  account: string;
  fee: number | null;
  feeCharged: string;
  classification: string;
  travel: string;
  ofac: string;
  posting: string;
  comments: string;
}

interface DraftPayload {
  sheetName: string;
  headerRow: number;
  samples: DraftSample[];
}

function main(workbook: ExcelScript.Workbook, expectedDraftName: string, draftPayloadJson: string): string {
  if (!/^DRAFT_[A-Za-z0-9_-]+\.xlsx$/.test(expectedDraftName) || workbook.getName() !== expectedDraftName) {
    throw new Error("Select the exact new DRAFT workbook copy; the source workbook is not an allowed target.");
  }
  if (typeof draftPayloadJson !== "string" || draftPayloadJson.length > 100000) {
    throw new Error("The draft payload is missing or exceeds its bounded size.");
  }
  const payload = JSON.parse(draftPayloadJson) as DraftPayload;
  if (!payload || typeof payload !== "object" || typeof payload.sheetName !== "string" ||
    !Number.isInteger(payload.headerRow) || payload.headerRow < 1 || payload.headerRow > 100 ||
    !Array.isArray(payload.samples) || payload.samples.length < 1 || payload.samples.length > 20) {
    throw new Error("Provide one sheet, its 1-based header row and 1 to 20 draft sample rows.");
  }
  const sheet = workbook.getWorksheet(payload.sheetName);
  if (!sheet) throw new Error("The explicitly selected worksheet was not found.");
  const used = sheet.getUsedRange(true);
  if (!used) throw new Error("The selected worksheet is empty.");
  const lastRow = used.getRowIndex() + used.getRowCount();
  const normalize = (text: string) => String(text).toLowerCase().replace(/[^a-z0-9#]/g, "");
  const headers = sheet.getRangeByIndexes(payload.headerRow - 1, 0, 1, 12).getValues()[0].map(value => normalize(String(value)));
  const aliases = [
    ["#", "sample", "sampleid", "samplenumber", "sample#"],
    ["date", "datetime", "wiredate", "transactiondatetime"],
    ["customer", "customername", "member", "membername", "beneficiaryname"],
    ["account", "accountnumber", "account#", "acct"],
    ["amount", "wireamount", "principalamount"],
    ["fee", "wirefee", "feeamount"],
    ["feecharged", "feechargedcorrectly", "feetest"],
    ["domesticforeign", "classification", "wireclassification"],
    ["travel", "travelrule", "travelrulereview"],
    ["ofac", "ofacreview"],
    ["transactionpostedcorrectlytoacct", "transactionpostedcorrectlytoaccount", "postedcorrectly", "posting"],
    ["comments", "comment", "reviewcomments", "notes"]
  ];
  if (headers.some((header, index) => !aliases[index].includes(header))) {
    throw new Error("This writer requires the reviewed A:L incoming-wire layout. Map and revalidate a different template explicitly.");
  }
  const rows = new Set<number>();
  const ids = new Set<string>();
  const marks = ["X", "-", "OPEN"];
  // Every target is validated before the first mutation.
  const plans = payload.samples.map(item => {
    if (!item || !Number.isInteger(item.row) || item.row <= payload.headerRow || item.row > lastRow || rows.has(item.row) ||
      typeof item.sampleId !== "string" || !item.sampleId.trim() || ids.has(item.sampleId) ||
      typeof item.customer !== "string" || !item.customer.trim() || typeof item.account !== "string" || !item.account.trim()) {
      throw new Error("A row has a missing, duplicate or out-of-range sample identity.");
    }
    rows.add(item.row);
    ids.add(item.sampleId);
    const context = sheet.getRangeByIndexes(item.row - 1, 0, 1, 4).getValues()[0];
    const same = (left: string, right: string) => left.trim().toUpperCase().replace(/\s+/g, " ") === right.trim().toUpperCase().replace(/\s+/g, " ");
    if (!same(String(context[0]), item.sampleId) || !same(String(context[2]), item.customer) || !same(String(context[3]), item.account)) {
      throw new Error("A target row does not match the expected sample ID, customer and account. No cells were written.");
    }
    if ((item.fee !== null && (typeof item.fee !== "number" || !Number.isFinite(item.fee) || item.fee < 0)) ||
      !marks.includes(item.feeCharged) || !marks.includes(item.travel) || !marks.includes(item.ofac) || !marks.includes(item.posting) ||
      !["Domestic", "Foreign", "OPEN"].includes(item.classification) || typeof item.comments !== "string" ||
      !item.comments.startsWith("DRAFT.") || item.comments.length > 1000) {
      throw new Error("A result has an invalid fee, classification, mark or draft comment.");
    }
    const target = sheet.getRangeByIndexes(item.row - 1, 5, 1, 7);
    if (target.getFormulas()[0].some(value => typeof value === "string" && value.startsWith("="))) {
      throw new Error("A testing target contains a formula. Explicitly remap and review before replacing formulas.");
    }
    const values = [item.fee === null ? "OPEN" : item.fee, item.feeCharged, item.classification,
      item.travel, item.ofac, item.posting, item.comments];
    return { row: item.row, values: values };
  });
  plans.forEach(plan => sheet.getRangeByIndexes(plan.row - 1, 5, 1, 7).setValues([plan.values]));
  return JSON.stringify({ status: "DRAFT", workbook: expectedDraftName, samplesWritten: plans.length,
    cellsWritten: plans.length * 7, humanReviewRequired: true });
}
