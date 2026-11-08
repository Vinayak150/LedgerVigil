import type { components, paths } from "./api.generated";
type Proposal = components["schemas"]["Proposal"];
export type EvidenceIdentity = Pick<
  components["schemas"]["EvidenceRef"],
  "kind" | "id"
>;
export type DraftContent = Pick<Proposal, "operations" | "rationale"> & {
  evidence_refs: EvidenceIdentity[];
};
export type Operation = Proposal["operations"][number];
export type Statement = components["schemas"]["StagedDocument"];
type PatchBody = NonNullable<
  paths["/api/v1/proposals/{proposal_id}"]["patch"]["requestBody"]
>["content"]["application/json"];
export function evidenceIdentity(ref: EvidenceIdentity): EvidenceIdentity {
  return { kind: ref.kind, id: ref.id };
}
export function patchContent(content: DraftContent): PatchBody {
  if (content.evidence_refs.length > 100)
    throw new Error("Use at most 100 evidence references.");
  const refs: NonNullable<PatchBody["evidence_refs"]> =
    content.evidence_refs.map((ref) => {
      const { kind, id } = ref;
      if (
        kind !== "SOURCE_RECORD" &&
        kind !== "STAGED_DOCUMENT" &&
        kind !== "DOCUMENT_VERSION" &&
        kind !== "ATTACHMENT"
      )
        throw new Error(
          "An evidence kind is unsupported. Refresh the draft before saving.",
        );
      return { kind, id };
    });
  if (new Set(refs.map((e) => `${e.kind}:${e.id}`)).size !== refs.length)
    throw new Error("Evidence references must be distinct.");
  return {
    operations: structuredClone(content.operations),
    rationale: content.rationale,
    evidence_refs: refs,
  };
}
export function replaceReferences(
  content: DraftContent,
  index: number,
  ledgerId: string,
  items: Operation["items"],
  evidence: EvidenceIdentity[],
  statement: Statement | null,
  adoptValues: boolean,
): DraftContent {
  const current = content.operations[index];
  if (!current || !items.length || items.length > 1000)
    throw new Error(
      "Keep at least one payment and at most 1,000 payment items.",
    );
  if (new Set(items.map((i) => i.document_id)).size !== items.length)
    throw new Error("Payment references must be distinct.");
  const currency = current.gross.currency;
  if (items.some((i) => i.kind !== "PAYMENT" || i.amount.currency !== currency))
    throw new Error("Use payment records in the ledger currency.");
  if (
    statement &&
    (statement.ledger_id !== ledgerId ||
      statement.gross.currency !== currency ||
      statement.fee.currency !== currency ||
      statement.net.currency !== currency)
  )
    throw new Error(
      "The statement must belong to this ledger and its currency.",
    );
  const next = structuredClone(content);
  next.evidence_refs = evidence.map(evidenceIdentity);
  next.operations[index] = {
    ...next.operations[index],
    items: structuredClone(items),
    ...(statement ? { statement_document_id: statement.id } : {}),
    ...(statement && adoptValues
      ? {
          processor_ref: statement.processor_ref,
          payout_id: statement.payout_id,
          payout_date: statement.payout_date,
          gross: { ...statement.gross },
          fee: { ...statement.fee },
          net: { ...statement.net },
        }
      : {}),
  };
  patchContent(next);
  return next;
}
