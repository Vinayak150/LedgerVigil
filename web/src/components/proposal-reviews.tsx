"use client";
import { useCallback, useState, type ComponentProps } from "react";
import { ProposalApproval } from "./proposal-approval";
import { ProposalExecution } from "./proposal-execution";
import { ProposalRejection } from "./proposal-rejection";
type Props = Omit<
  ComponentProps<typeof ProposalApproval>,
  "peerBlocked" | "onLockChange"
>;
export function ProposalReviews(props: Props) {
  const [locks, setLocks] = useState({
    approval: false,
    rejection: false,
    execution: false,
  });
  const approvalLock = useCallback(
    (value: boolean) =>
      setLocks((s) => (s.approval === value ? s : { ...s, approval: value })),
    [],
  );
  const rejectionLock = useCallback(
    (value: boolean) =>
      setLocks((s) => (s.rejection === value ? s : { ...s, rejection: value })),
    [],
  );
  const executionLock = useCallback(
    (value: boolean) =>
      setLocks((s) => (s.execution === value ? s : { ...s, execution: value })),
    [],
  );
  return (
    <>
      <ProposalExecution
        key={`execute:${props.tenantId}:${props.userId}:${props.proposal.id}`}
        {...props}
        peerBlocked={locks.approval || locks.rejection}
        onLockChange={executionLock}
      />
      <ProposalApproval
        {...props}
        peerBlocked={locks.rejection || locks.execution}
        onLockChange={approvalLock}
      />
      <ProposalRejection
        {...props}
        peerBlocked={locks.approval || locks.execution}
        onLockChange={rejectionLock}
      />
    </>
  );
}
