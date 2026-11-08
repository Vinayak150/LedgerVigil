"use client";
import { useEffect, useRef, useState } from "react";
import { Download } from "lucide-react";
import { api } from "@/lib/api";
import { type ProofTarget } from "@/lib/execution-proof";
import { Button } from "./button";
export function ExecutionProofDownload({
  target,
  disabled = false,
}: {
  target: ProofTarget;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [fileHash, setFileHash] = useState("");
  const requestKey = useRef(`proof-export-${target.executionId}`);
  const sending = useRef(false),
    active = useRef<AbortController | null>(null),
    status = useRef<HTMLParagraphElement>(null);
  useEffect(
    () => () => active.current?.abort(),
    [target.tenantId, target.executionId],
  );
  async function download() {
    if (disabled || sending.current) return;
    sending.current = true;
    setBusy(true);
    setMessage("");
    const controller = new AbortController();
    active.current = controller;
    try {
      const session = await api.GET("/api/v1/me", {
        signal: controller.signal,
      });
      if (!session.data || session.data.tenant?.id !== target.tenantId)
        throw Error(
          "Your organization changed. Refresh this proposal before exporting.",
        );
      if (!session.data.permissions.includes("evidence.export"))
        throw Error(
          "Downloading financial evidence requires export permission.",
        );
      const headers = { "X-CSRF-Token": session.data.csrf_token };
      const requested = await api.POST(
        "/api/v1/proposals/{proposal_id}/proof-packs",
        {
          signal: controller.signal,
          params: { path: { proposal_id: target.proposalId } },
          headers: {
            ...headers,
            "Idempotency-Key": `pack-${target.executionId}`,
          },
          body: { revision: target.revision },
        },
      );
      if (!requested.data)
        throw Error(
          "The proof pack could not be requested. Refresh your session and access.",
        );
      const packId = requested.data.proof_pack.id;
      async function pause() {
        controller.signal.throwIfAborted();
        await new Promise<void>((resolve, reject) => {
          const stop = () => {
            clearTimeout(timer);
            reject(new DOMException("Aborted", "AbortError"));
          };
          const timer = setTimeout(() => {
            controller.signal.removeEventListener("abort", stop);
            resolve();
          }, 1000);
          controller.signal.addEventListener("abort", stop, { once: true });
        });
      }
      let anchored = false;
      for (let attempt = 0; attempt < 30; attempt++) {
        const result = await api.GET("/api/v1/proof-packs/{pack_id}", {
          signal: controller.signal,
          params: { path: { pack_id: packId } },
        });
        const pack = result.data;
        if (
          !pack ||
          pack.proposal_id !== target.proposalId ||
          pack.revision !== target.revision ||
          pack.ledger_id !== target.ledgerId
        )
          throw Error("Proof-pack scope or access changed. No file was saved.");
        if (pack.state === "SEALED" && pack.anchor_commit) {
          anchored = true;
          break;
        }
        if (pack.state === "DEAD" || pack.state === "ANCHOR_FAILED")
          throw Error(
            "The proof pack needs operator attention before it can be exported.",
          );
        setMessage(
          "Sealing the proof pack and recording its independent anchor…",
        );
        await pause();
      }
      if (!anchored)
        throw Error(
          "Proof-pack sealing or anchoring is still pending. Try again to check the same request.",
        );
      const created = await api.POST("/api/v1/proof-packs/{pack_id}/exports", {
        signal: controller.signal,
        params: { path: { pack_id: packId } },
        headers: { ...headers, "Idempotency-Key": requestKey.current },
        body: { format: "BUNDLE" },
      });
      if (!created.data)
        throw Error("The proof-pack export could not be authorized.");
      const jobId = created.data.id;
      let expectedHash: string | null = null;
      let expectedBytes: number | null = null;
      for (let attempt = 0; attempt < 30; attempt++) {
        const result = await api.GET(
          "/api/v1/execution-proof-exports/{job_id}",
          { signal: controller.signal, params: { path: { job_id: jobId } } },
        );
        const job = result.data;
        if (!job || job.proof_pack_id !== packId || job.format !== "BUNDLE")
          throw Error("Export scope or access changed.");
        if (job.state === "DONE") {
          expectedHash = job.content_hash;
          expectedBytes = job.bytes;
          break;
        }
        if (job.state === "DEAD" || job.state === "DENIED") {
          requestKey.current = crypto.randomUUID();
          throw Error(
            "Export failed. Check your access, then try again to request a fresh export.",
          );
        }
        setMessage("Preparing the JSON and readable HTML bundle…");
        await pause();
      }
      if (!expectedHash)
        throw Error(
          "Your export is still processing. Try again to check the same export.",
        );
      const signedLink = await api.POST(
        "/api/v1/execution-proof-exports/{job_id}/download-link",
        {
          signal: controller.signal,
          params: { path: { job_id: jobId } },
          headers,
        },
      );
      if (
        !signedLink.data ||
        !signedLink.data.url.startsWith(
          `/api/v1/execution-proof-exports/${jobId}/download?token=`,
        )
      )
        throw Error("A secure download link could not be issued.");
      const response = await fetch(signedLink.data.url, {
        signal: AbortSignal.any([
          controller.signal,
          AbortSignal.timeout(15_000),
        ]),
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
      if (!response.ok)
        throw Error(
          "The download or independent-anchor verification failed. No file was saved.",
        );
      const bytes = await response.arrayBuffer();
      if (controller.signal.aborted) return;
      if (
        bytes.byteLength !== expectedBytes ||
        bytes.byteLength > 16 * 1024 * 1024
      )
        throw Error("The bundle size does not match its stored record.");
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
        (b) => b.toString(16).padStart(2, "0"),
      ).join("");
      if (
        digest !== expectedHash ||
        digest !== response.headers.get("X-Proof-SHA256")
      )
        throw Error("The bundle digest could not be verified.");
      const verified = {
        bytes,
        digest,
        filename: `ledgervigil-proof-pack-${packId}.zip`,
      };
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(
          new Blob([verified.bytes as BlobPart], { type: "application/zip" }),
        ),
        link = document.createElement("a");
      link.href = url;
      link.download = verified.filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setFileHash(verified.digest);
      setMessage(
        "Proof pack download requested. Stored byte hash and proposal scope checked. Use the offline verifier with the independent Git repository to verify the canonical manifest, readable HTML and anchor. Accounting remains UNREVIEWED.",
      );
    } catch (e) {
      if (!controller.signal.aborted)
        setMessage(
          e instanceof Error
            ? e.message
            : "The bundle could not be verified. No file was saved.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) {
        setBusy(false);
        requestAnimationFrame(() => status.current?.focus());
      }
    }
  }
  return (
    <div className="execution-proof-export">
      <p className="small muted">
        Export the sealed revision, evidence and decisions as canonical JSON and
        readable HTML. The pack is anchored in an independent local Git
        repository and contains organization financial data; keep it private.
      </p>
      <Button
        type="button"
        variant="secondary"
        disabled={disabled || busy}
        onClick={() => void download()}
      >
        <Download size={16} />
        {busy ? "Preparing proof pack…" : "Download proof pack"}
      </Button>
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="notice">
          {message}
        </p>
      )}
      {fileHash && (
        <details className="record-technical">
          <summary>Last requested file digest</summary>
          <p>
            SHA-256 <code>{fileHash}</code>
          </p>
          <p className="small muted">
            This is the stored bundle digest, checked against its download. The
            offline verifier checks its manifest and independent Git anchor. Git
            anchoring is not a digital signature or accounting approval.
          </p>
        </details>
      )}
    </div>
  );
}
