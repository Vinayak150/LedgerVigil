import type { components } from "./api.generated";
import type { DraftContent } from "./draft-references";
export type Proposal = components["schemas"]["Proposal"];
export type EditableContent = DraftContent;
export type FieldIssue = { pointer: string; code: string };
export function editableContent(proposal: Proposal): EditableContent {
  return {
    operations: structuredClone(proposal.operations),
    rationale: proposal.rationale,
    evidence_refs: (proposal.evidence_refs ?? []).map(({ kind, id }) => ({
      kind,
      id,
    })),
  };
}
export function editChanged(
  before: EditableContent,
  after: EditableContent,
): boolean {
  return JSON.stringify(before) !== JSON.stringify(after);
}
export function fieldIssues(error: unknown): FieldIssue[] {
  if (
    !error ||
    typeof error !== "object" ||
    !("errors" in error) ||
    !Array.isArray(error.errors)
  )
    return [];
  return error.errors.filter(
    (e): e is FieldIssue =>
      !!e &&
      typeof e === "object" &&
      typeof e.pointer === "string" &&
      typeof e.code === "string",
  );
}
export function editError(status: number): string {
  if (status === 401)
    return "Your session has ended. Your changes are kept here while this editor remains open.";
  if (status === 403)
    return "Your account is not authorized to edit this draft. No edit was confirmed.";
  if (status === 404)
    return "This draft or one of its references is unavailable. No edit was confirmed.";
  if (status === 412)
    return "This draft changed after you opened it. Your edits are kept below; load the latest draft before editing again.";
  if (status === 409) return "This proposal is no longer an editable draft.";
  if (status === 422)
    return "Some fields were refused. Review the highlighted fields; no edit was saved.";
  if (status === 428)
    return "The draft version could not be verified. Reload its latest version before saving.";
  return "The save result could not be confirmed. Check the latest draft before making another change.";
}
export function validAmount(value: string): boolean {
  return /^[0-9]{1,16}(\.[0-9]{1,4})?$/.test(value);
}

export function issueText(code: string): string {
  if (code === "MONEY_SCALE_EXCEEDED")
    return "Use the decimal precision allowed by this currency.";
  if (code === "MONEY_MUST_BE_STRING")
    return "Enter the amount as a decimal value.";
  if (code === "CURRENCY_MISMATCH")
    return "The amount currency must match the ledger currency.";
  if (code === "not_found")
    return "The referenced record is unavailable in this ledger.";
  if (code === "PARTIAL_ITEM_SETTLEMENT")
    return "This profile requires the full payment amount.";
  if (code === "pattern") return "Check the format of this value.";
  if (code === "date") return "Enter a valid calendar date.";
  if (code === "rationale") return "Use at most 4,000 characters.";
  if (code.startsWith("Use ")) return code;
  return "Check this field and try again.";
}
export function issueLabel(pointer: string): string {
  if (pointer === "/rationale") return "Rationale";
  const op = pointer.match(
    /^\/operations\/(\d+)\/(processor_ref|payout_id|payout_date|gross|fee|net)/,
  );
  if (op) {
    const labels: Record<string, string> = {
      processor_ref: "Processor reference",
      payout_id: "Payout reference",
      payout_date: "Payout date",
      gross: "Proposed gross",
      fee: "Proposed fee",
      net: "Proposed net",
    };
    return `Operation ${Number(op[1]) + 1}: ${labels[op[2]]}`;
  }
  if (pointer.startsWith("/evidence_refs")) return "Evidence reference";
  if (pointer.includes("/items/")) return "Payment reference";
  return "Draft";
}
