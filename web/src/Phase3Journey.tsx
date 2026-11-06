import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Scenario } from "./scenarios";
import type {
  AuditResponse,
  CreateRunResponse,
  RunView,
} from "./api-types";

const money = (pence: number | null | undefined) =>
  pence == null
    ? "—"
    : new Intl.NumberFormat("en-GB", {
        style: "currency",
        currency: "GBP",
        maximumFractionDigits: 2,
      }).format(pence / 100);

function errorText(payload: unknown, status: number): string {
  const value = payload as {
    error?: { code?: string; message?: string };
    detail?: { code?: string; message?: string } | string;
  };
  if (value?.error?.code) {
    return `${value.error.code}: ${value.error.message ?? ""}`.trim();
  }
  if (typeof value?.detail === "string") {
    return value.detail;
  }
  if (value?.detail?.code) {
    return `${value.detail.code}: ${value.detail.message ?? ""}`.trim();
  }
  return `Request failed with HTTP ${status}`;
}

type Props = {
  scenario: Scenario;
  budgetPence: number;
};

export default function Phase3Journey({ scenario, budgetPence }: Props) {
  const [plannerToken, setPlannerToken] = useState("");
  const [managerToken, setManagerToken] = useState("");
  const [run, setRun] = useState<RunView | null>(null);
  const [audit, setAudit] = useState<AuditResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const [decisionKey, setDecisionKey] = useState("demo-decision-1");
  const [existingRunId, setExistingRunId] = useState("");
  const sequence = useRef(0);
  const refreshSequence = useRef(0);
  const auditSequence = useRef(0);
  const currentRunId = useRef<string | null>(null);
  const requestAbort = useRef<AbortController | null>(null);

  const headers = (token: string, withIdempotency?: string) => ({
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
    ...(withIdempotency ? { "Idempotency-Key": withIdempotency } : {}),
  });

  async function loadAudit(runId: string, token: string) {
    const auditId = ++auditSequence.current;
    const refreshId = refreshSequence.current;
    try {
      const response = await fetch(`/v1/runs/${runId}/audit`, {
        headers: headers(token),
      });
      if (
        auditId !== auditSequence.current ||
        refreshId !== refreshSequence.current ||
        currentRunId.current !== runId
      ) {
        return;
      }
      if (response.ok) {
        setAudit((await response.json()) as AuditResponse);
      } else {
        setAudit(null);
      }
    } catch {
      if (auditId === auditSequence.current) setAudit(null);
    }
  }

  async function refresh(runId: string, token: string, replace = false) {
    const refreshId = ++refreshSequence.current;
    if (replace) {
      currentRunId.current = runId;
      setRun(null);
      setAudit(null);
      setError(null);
    }
    const response = await fetch(`/v1/runs/${runId}`, {
      headers: headers(token),
    });
    const payload = await response.json();
    if (refreshId !== refreshSequence.current) return;
    if (!response.ok) {
      setError(errorText(payload, response.status));
      setAudit(null);
      if (replace) {
        currentRunId.current = null;
        setRun(null);
      }
      return;
    }
    const next = payload as RunView;
    if (currentRunId.current !== next.run_id) {
      currentRunId.current = next.run_id;
      setAudit(null);
    }
    setRun(next);
    if (
      [
        "needs_input",
        "awaiting_approval",
        "completed",
        "degraded",
        "rejected",
        "failed",
        "expired",
      ].includes(next.status)
    ) {
      await loadAudit(runId, token);
    }
  }

  function changePlannerToken(value: string) {
    setPlannerToken(value);
    refreshSequence.current += 1;
    auditSequence.current += 1;
    currentRunId.current = null;
    setRun(null);
    setAudit(null);
    setError(null);
  }

  function changeManagerToken(value: string) {
    setManagerToken(value);
    auditSequence.current += 1;
    setAudit(null);
    setError(null);
  }

  useEffect(() => {
    if (!run || !plannerToken) return;
    if (!["queued", "running", "approved", "rejected"].includes(run.status)) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await refresh(run.run_id, plannerToken);
    };
    const interval = window.setInterval(() => {
      void tick();
    }, 750);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [run?.run_id, run?.status, plannerToken]);

  async function startRun() {
    const sequenceId = ++sequence.current;
    refreshSequence.current += 1;
    requestAbort.current?.abort();
    const controller = new AbortController();
    requestAbort.current = controller;
    setBusy(true);
    setError(null);
    setAudit(null);
    currentRunId.current = null;
    const project = {
      ...scenario.project,
      budget_pence: scenario.budgetEditable
        ? budgetPence
        : scenario.project.budget_pence,
    };
    const draft = {
      ...project,
      disruption: scenario.disruption,
    };
    try {
      const response = await fetch("/v1/runs", {
        method: "POST",
        headers: {
          ...headers(plannerToken, `create-${scenario.id}-${sequenceId}`),
        },
        body: JSON.stringify({ draft, scenario_id: scenario.id }),
        signal: controller.signal,
      });
      const payload = await response.json();
      if (sequenceId !== sequence.current) return;
      if (!response.ok) {
        setError(errorText(payload, response.status));
        return;
      }
      const created = payload as CreateRunResponse;
      await refresh(created.run_id, plannerToken, true);
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Run submission failed");
      }
    } finally {
      if (sequenceId === sequence.current) setBusy(false);
    }
  }

  async function submitClarification(event: FormEvent) {
    event.preventDefault();
    if (!run?.pending_question) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/v1/runs/${run.run_id}/clarifications`, {
        method: "POST",
        headers: headers(plannerToken),
        body: JSON.stringify({
          field: run.pending_question.field,
          answer: clarificationAnswer,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setError(errorText(payload, response.status));
        return;
      }
      setClarificationAnswer("");
      await refresh(run.run_id, plannerToken);
    } finally {
      setBusy(false);
    }
  }

  async function decide(approved: boolean, staleDemo = false) {
    if (!run?.pending_decision) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/v1/runs/${run.run_id}/decisions`, {
        method: "POST",
        headers: headers(
          managerToken,
          staleDemo ? `${decisionKey}-stale-demo` : decisionKey,
        ),
        body: JSON.stringify({
          candidate_id: run.pending_decision.candidate_id,
          project_version: staleDemo
            ? run.pending_decision.project_version + 1
            : run.pending_decision.project_version,
          input_hash: run.pending_decision.input_hash,
          catalogue_hash: run.pending_decision.catalogue_hash,
          approved,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setError(errorText(payload, response.status));
        return;
      }
      await refresh(run.run_id, plannerToken);
      await loadAudit(run.run_id, managerToken);
    } finally {
      setBusy(false);
    }
  }

  const pending = run?.pending_decision;
  const evaluation = run?.evaluation;
  const recommended = evaluation?.candidates.find(
    (candidate) => candidate.candidate_id === evaluation.recommended_candidate_id,
  );

  return (
    <section className="panel phase3" aria-labelledby="phase3-heading">
      <h2 id="phase3-heading">Durable authorised journey</h2>
      <p className="hint">
        Tokens are held only in this browser session. The server derives the actor and
        roles; a request body cannot grant manager authority.
      </p>
      <div className="field">
        <label htmlFor="planner-token">Planner bearer token (session only)</label>
        <input
          id="planner-token"
          type="password"
          autoComplete="off"
          value={plannerToken}
          onChange={(event) => changePlannerToken(event.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="manager-token">Manager bearer token (session only)</label>
        <input
          id="manager-token"
          type="password"
          autoComplete="off"
          value={managerToken}
          onChange={(event) => changeManagerToken(event.target.value)}
        />
      </div>
      <button type="button" onClick={() => void startRun()} disabled={busy || !plannerToken}>
        {busy ? "Working…" : "Start durable run"}
      </button>
      <div className="field">
        <label htmlFor="existing-run">Load an existing run ID</label>
        <input
          id="existing-run"
          value={existingRunId}
          onChange={(event) => setExistingRunId(event.target.value)}
        />
        <button
          type="button"
          onClick={() => {
            if (existingRunId.trim()) {
              setError(null);
              void refresh(existingRunId.trim(), plannerToken, true);
            }
          }}
          disabled={busy || !plannerToken || !existingRunId.trim()}
        >
          Load run
        </button>
      </div>

      {error && (
        <div className="error" role="alert" aria-live="assertive">
          <h3>Run could not continue</h3>
          <p>{error}</p>
        </div>
      )}

      {run && (
        <div className="journey">
          <dl className="run-identity">
            <div>
              <dt>Run</dt>
              <dd>
                <code>{run.run_id}</code>
              </dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>
                <span className={`status status-${run.status}`}>{run.status}</span>
              </dd>
            </div>
            <div>
              <dt>Stable thread</dt>
              <dd>
                <code>{run.thread_id}</code>
              </dd>
            </div>
            <div>
              <dt>Committed revision</dt>
              <dd>{run.revision}</dd>
            </div>
          </dl>

          {run.status === "needs_input" && run.pending_question && (
            <form className="clarification" onSubmit={(event) => void submitClarification(event)}>
              <h3>Clarification required</h3>
              <p>{run.pending_question.question}</p>
              <label htmlFor="clarification-answer">Answer</label>
              <input
                id="clarification-answer"
                value={clarificationAnswer}
                onChange={(event) => setClarificationAnswer(event.target.value)}
                required
              />
              <button type="submit" disabled={busy}>
                Submit clarification
              </button>
            </form>
          )}

          {evaluation && (
            <div className="panel authoritative">
              <h3>Authoritative schedule and cost facts</h3>
              <div className="cards">
                <article className="card">
                  <h4>Baseline</h4>
                  <p className="metric">Day {evaluation.baseline.finish_offset_workdays}</p>
                </article>
                <article className="card disrupted">
                  <h4>Disrupted</h4>
                  <p className="metric">Day {evaluation.disrupted.finish_offset_workdays}</p>
                </article>
                <article className="card recommended">
                  <h4>Recovery</h4>
                  <p className="metric">
                    {pending?.finish_offset_workdays ??
                      recommended?.finish_offset_workdays ??
                      "—"}
                  </p>
                  <p className="muted">
                    {money(pending?.cost_pence ?? recommended?.cost_pence)}
                  </p>
                </article>
              </div>
              <table>
                <caption>Recovery alternatives from the deterministic engine</caption>
                <thead>
                  <tr>
                    <th scope="col">Options</th>
                    <th scope="col">Finish</th>
                    <th scope="col">Saving</th>
                    <th scope="col">Cost</th>
                    <th scope="col">Feasible</th>
                  </tr>
                </thead>
                <tbody>
                  {evaluation.candidates.map((candidate) => (
                    <tr key={candidate.candidate_id}>
                      <th scope="row">{candidate.option_ids.join(" + ") || "No change"}</th>
                      <td>Day {candidate.finish_offset_workdays}</td>
                      <td>{candidate.project_saving_workdays} days</td>
                      <td>{money(candidate.cost_pence)}</td>
                      <td>{candidate.feasible ? "Yes" : `No (${candidate.rejection_codes.join(", ")})`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {pending && run.status === "awaiting_approval" && (
            <div className="approval" aria-labelledby="approval-heading">
              <h3 id="approval-heading">Awaiting manager decision</h3>
              <p>
                Candidate <code>{pending.candidate_id}</code> · version{" "}
                {pending.project_version} · finish {pending.finish_offset_workdays} ·{" "}
                {money(pending.cost_pence)}
              </p>
              <div className="field">
                <label htmlFor="decision-key">Decision idempotency key</label>
                <input
                  id="decision-key"
                  value={decisionKey}
                  onChange={(event) => setDecisionKey(event.target.value)}
                />
              </div>
              <button
                type="button"
                onClick={() => void decide(true)}
                disabled={busy || !managerToken}
              >
                Approve recovery
              </button>
              <button
                type="button"
                onClick={() => void decide(false)}
                disabled={busy || !managerToken}
              >
                Reject recovery
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => void decide(true, true)}
                disabled={busy || !managerToken}
              >
                Attempt stale decision (demo)
              </button>
            </div>
          )}

          {run.explanation && (
            <article
              className="panel explanation"
              aria-label="Pre-decision recommendation"
            >
              <h3>
                Pre-decision recommendation (snapshot; prose, not the source of
                schedule facts)
              </h3>
              <p>{run.explanation.summary}</p>
            </article>
          )}

          {run.decision_status === "approved" && (
            <div className="panel decision" role="status">
              <h3>Decision status: approved</h3>
              <p>
                Approved by an authorised manager. Revision {run.revision} is
                committed with the revised plan and audit event.
              </p>
            </div>
          )}
          {run.decision_status === "rejected" && (
            <div className="panel decision" role="status">
              <h3>Decision status: rejected</h3>
              <p>Recovery was rejected; no revised plan was applied.</p>
            </div>
          )}

          {run.status === "degraded" && (
            <div className="panel stale" role="status">
              <h3>Degraded result</h3>
              <p>
                Deterministic facts remain available. Provider reason:{" "}
                {run.provider_outcome?.reason_code ?? "unknown"}.
              </p>
            </div>
          )}

          {run.status === "rejected" && (
            <div className="panel stale" role="status">
              <h3>Recovery rejected</h3>
              <p>The approved plan was not changed; no revision was applied.</p>
            </div>
          )}

          {audit && (
            <div className="panel revision-history">
              <h3>Revision and audit history</h3>
              <p>
                {audit.revisions.length} committed revision
                {audit.revisions.length === 1 ? "" : "s"}.
              </p>
              <ol>
                {audit.events.map((event) => (
                  <li key={event.event_id}>
                    {event.event_type} · {event.actor_id ?? "system"} ·{" "}
                    {event.created_at}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
