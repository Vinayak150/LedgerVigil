export const PROOF_FORMAT = "ledgervigil.native-execution-proof/1";
export const PROOF_LIMIT = 16 * 1024 * 1024;
export type ProofTarget = {
  tenantId: string;
  ledgerId: string;
  proposalId: string;
  revision: number;
  executionId: string;
  simulationId: string;
  commitSeq: string;
};
function canonical(v: unknown): string {
  if (v === null || typeof v === "boolean") return JSON.stringify(v);
  if (typeof v === "string")
    return JSON.stringify(v).replace(
      /[\u007f-\uffff]/g,
      (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
    );
  if (typeof v === "number" && Number.isSafeInteger(v))
    return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  if (v && typeof v === "object")
    return (
      "{" +
      Object.keys(v)
        .sort()
        .map(
          (k) =>
            canonical(k) + ":" + canonical((v as Record<string, unknown>)[k]),
        )
        .join(",") +
      "}"
    );
  throw Error("Unsupported integrity bundle value");
}
async function digest(bytes: Uint8Array) {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", bytes as BufferSource),
    ),
  ]
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
}
const object = (v: unknown): Record<string, unknown> => {
  if (!v || typeof v !== "object" || Array.isArray(v))
    throw Error("Invalid integrity bundle");
  return v as Record<string, unknown>;
};
export async function checkProofDownload(
  text: string,
  header: string | null,
  target: ProofTarget,
) {
  const bytes = new TextEncoder().encode(text);
  if (
    !bytes.length ||
    bytes.length > PROOF_LIMIT ||
    !header ||
    !/^[a-f0-9]{64}$/.test(header) ||
    !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(
      target.executionId,
    )
  )
    throw Error("Missing or invalid bundle integrity metadata");
  if ((await digest(bytes)) !== header)
    throw Error("Downloaded bytes do not match the response digest");
  const b = object(JSON.parse(text)),
    s = object(b.scope),
    claims = object(b.claims),
    artifacts = object(b.artifacts);
  if (
    Object.keys(b).sort().join() !==
      ["format", "scope", "claims", "manifest", "manifest_sha256", "artifacts"]
        .sort()
        .join() ||
    b.format !== PROOF_FORMAT ||
    canonical(b) + "\n" !== text
  )
    throw Error("Unsupported or noncanonical integrity bundle");
  const expected = {
    tenant_id: target.tenantId,
    ledger_id: target.ledgerId,
    proposal_id: target.proposalId,
    revision: target.revision,
    execution_id: target.executionId,
    simulation_id: target.simulationId,
    commit_seq: target.commitSeq,
  };
  if (Object.entries(expected).some(([k, v]) => s[k] !== v))
    throw Error(
      "Bundle does not match this organization and execution receipt",
    );
  if (
    claims.integrity !== "INTERNAL_CONSISTENCY_ONLY" ||
    claims.accounting !== "UNREVIEWED" ||
    claims.signature !== "NONE" ||
    claims.external_anchor !== "NONE"
  )
    throw Error("Unsupported integrity claims");
  const names = [
    "approval.json",
    "documents.json",
    "execution.json",
    "history.json",
    "inclusions.json",
    "journal.json",
    "revision.json",
    "simulation.json",
  ];
  if (
    Object.keys(artifacts).sort().join() !== names.join() ||
    !Array.isArray(b.manifest) ||
    b.manifest.length !== names.length
  )
    throw Error("Incomplete integrity bundle");
  for (let i = 0; i < names.length; i++) {
    const m = object(b.manifest[i]),
      body = new TextEncoder().encode(canonical(artifacts[names[i]]));
    if (
      m.name !== names[i] ||
      m.bytes !== body.length ||
      m.sha256 !== (await digest(body))
    )
      throw Error("Integrity manifest does not match its records");
  }
  const core = {
    format: b.format,
    scope: b.scope,
    claims: b.claims,
    manifest: b.manifest,
  };
  if (
    (await digest(new TextEncoder().encode(canonical(core)))) !==
    b.manifest_sha256
  )
    throw Error("Integrity manifest hash mismatch");
  return {
    bytes,
    digest: header,
    filename: `ledgervigil-execution-${target.executionId}.json`,
  };
}
