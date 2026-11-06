import { useMemo, useRef, useState } from "react";
import Phase3Journey from "./Phase3Journey";
import { SCENARIOS, type Scenario } from "./scenarios";

type Facts = {
  finish_offset_workdays: number;
  elapsed_workdays: number;
  critical_task_ids: string[];
};

type Candidate = {
  candidate_id: string;
  option_ids: string[];
  task_reductions: Record<string, number>;
  project_saving_workdays: number;
  finish_offset_workdays: number;
  cost_pence: number;
  feasible: boolean;
  rejection_codes: string[];
};

type Evaluation = {
  baseline: Facts;
  disrupted: Facts;
  candidates: Candidate[];
  recommended_candidate_id: string | null;
  ranked_feasible_candidate_ids: string[];
};

const money = (pence: number) =>
  new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: "GBP",
    maximumFractionDigits: 2,
  }).format(pence / 100);

function optionLabel(candidate: Candidate): string {
  return candidate.option_ids.length ? candidate.option_ids.join(" + ") : "No change";
}

function rejectionLabel(codes: string[]): string {
  return codes.length ? codes.join(", ") : "—";
}

export default function App() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[0].id);
  const scenario = useMemo(
    () => SCENARIOS.find((item) => item.id === scenarioId) ?? SCENARIOS[0],
    [scenarioId],
  );
  const [budgetPence, setBudgetPence] = useState(scenario.project.budget_pence);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [staleNotice, setStaleNotice] = useState(false);
  const requestSequence = useRef(0);
  const abortController = useRef<AbortController | null>(null);

  function invalidateResults(showNotice: boolean) {
    requestSequence.current += 1;
    abortController.current?.abort();
    abortController.current = null;
    setEvaluation(null);
    setLoading(false);
    setStaleNotice(showNotice);
  }

  function chooseScenario(next: Scenario) {
    setScenarioId(next.id);
    setBudgetPence(next.project.budget_pence);
    setError(null);
    invalidateResults(false);
  }

  function changeBudget(nextPence: number) {
    setBudgetPence(nextPence);
    if (evaluation || loading) {
      invalidateResults(true);
    }
  }

  async function runEvaluation() {
    const sequence = ++requestSequence.current;
    abortController.current?.abort();
    const controller = new AbortController();
    abortController.current = controller;
    setLoading(true);
    setError(null);
    setStaleNotice(false);
    const project = {
      ...scenario.project,
      budget_pence: scenario.budgetEditable ? budgetPence : scenario.project.budget_pence,
    };

    try {
      const response = await fetch("/v1/recovery/evaluations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project,
          disruption: scenario.disruption,
          option_ids: null,
        }),
        signal: controller.signal,
      });
      const payload = await response.json();
      if (sequence !== requestSequence.current) {
        return;
      }
      if (!response.ok) {
        const detail = payload?.error
          ? `${payload.error.code}: ${payload.error.message}`
          : JSON.stringify(payload?.detail ?? payload);
        throw new Error(detail);
      }
      setEvaluation(payload as Evaluation);
    } catch (caught) {
      if (controller.signal.aborted || sequence !== requestSequence.current) {
        return;
      }
      setEvaluation(null);
      setError(caught instanceof Error ? caught.message : "Evaluation failed");
    } finally {
      if (sequence === requestSequence.current) {
        setLoading(false);
        abortController.current = null;
      }
    }
  }

  const recommended = evaluation?.candidates.find(
    (candidate) => candidate.candidate_id === evaluation.recommended_candidate_id,
  );

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">Focused technical review · synthetic scenario</p>
        <h1>Delivery Recovery Assistant</h1>
        <p className="boundary">
          The CPM engine supplies every authoritative finish, saving and cost.
          Explanation prose is separate. Durable runs add server-authorised
          manager approval, committed revisions and audit.
        </p>
      </header>

      <section className="panel controls" aria-labelledby="controls-heading">
        <h2 id="controls-heading">Scenario</h2>
        <label htmlFor="scenario">Delivery scenario</label>
        <select
          id="scenario"
          value={scenarioId}
          onChange={(event) => {
            const next = SCENARIOS.find((item) => item.id === event.target.value);
            if (next) chooseScenario(next);
          }}
        >
          {SCENARIOS.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
        <p className="description">{scenario.description}</p>

        {scenario.budgetEditable && (
          <div className="field">
            <label htmlFor="budget">Recovery budget (GBP)</label>
            <input
              id="budget"
              type="number"
              min="0"
              step="100"
              value={budgetPence / 100}
              onChange={(event) => {
                const pounds = Number(event.target.value);
                changeBudget(
                  Number.isFinite(pounds) ? Math.max(0, Math.round(pounds * 100)) : 0,
                );
              }}
            />
            <p className="hint">Budget equality is eligible; one penny below is not.</p>
          </div>
        )}

        <button type="button" onClick={() => void runEvaluation()} disabled={loading}>
          {loading ? "Evaluating…" : "Evaluate recovery"}
        </button>
      </section>

      {staleNotice && (
        <section className="panel stale" role="status" aria-live="polite">
          <h2>Inputs changed</h2>
          <p>Evaluate again to refresh the recommendation for the current scenario and budget.</p>
        </section>
      )}

      {error && (
        <section className="panel error" role="alert" aria-live="assertive">
          <h2>Evaluation failed</h2>
          <p>{error}</p>
        </section>
      )}

      {evaluation && (
        <section className="results" aria-live="polite">
          <div className="cards">
            <article className="card">
              <h2>Baseline</h2>
              <p className="metric">Day {evaluation.baseline.finish_offset_workdays}</p>
              <p className="muted">
                {evaluation.baseline.elapsed_workdays} working days · critical{" "}
                {evaluation.baseline.critical_task_ids.join(", ")}
              </p>
            </article>
            <article className="card disrupted">
              <h2>Disrupted</h2>
              <p className="metric">Day {evaluation.disrupted.finish_offset_workdays}</p>
              <p className="muted">
                {evaluation.disrupted.elapsed_workdays} working days · critical{" "}
                {evaluation.disrupted.critical_task_ids.join(", ")}
              </p>
            </article>
            <article className={`card ${recommended ? "recommended" : "none"}`}>
              <h2>Recommendation</h2>
              <p className="metric">
                {recommended ? optionLabel(recommended) : "No feasible option"}
              </p>
              <p className="muted">
                {recommended
                  ? `${money(recommended.cost_pence)} · finish day ${recommended.finish_offset_workdays}`
                  : "The target and budget are not relaxed."}
              </p>
            </article>
          </div>

          <div className="panel">
            <h2>Verified candidates</h2>
            <p className="hint">
              Every saving is recomputed against the disrupted plan. Candidate IDs are stable
              hashes of the exact plan, catalogue and option selection.
            </p>
            <table>
              <caption className="sr-only">Recovery candidates with recomputed facts</caption>
              <thead>
                <tr>
                  <th scope="col">Options</th>
                  <th scope="col">Task reductions</th>
                  <th scope="col">Finish</th>
                  <th scope="col">Project saving</th>
                  <th scope="col">Cost</th>
                  <th scope="col">Status</th>
                  <th scope="col">Rejection</th>
                </tr>
              </thead>
              <tbody>
                {evaluation.candidates.map((candidate) => (
                  <tr
                    key={candidate.candidate_id}
                    className={candidate.feasible ? "row-feasible" : "row-rejected"}
                  >
                    <th scope="row">
                      {optionLabel(candidate)}
                      {candidate.candidate_id === evaluation.recommended_candidate_id && (
                        <span className="tag">Recommended</span>
                      )}
                    </th>
                    <td>
                      {Object.entries(candidate.task_reductions)
                        .map(([task, days]) => `${task} −${days}`)
                        .join(", ") || "—"}
                    </td>
                    <td>Day {candidate.finish_offset_workdays}</td>
                    <td>{candidate.project_saving_workdays} days</td>
                    <td>{money(candidate.cost_pence)}</td>
                    <td>
                      <span className="status">
                        {candidate.feasible ? "Feasible" : "Rejected"}
                      </span>
                    </td>
                    <td>{rejectionLabel(candidate.rejection_codes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <Phase3Journey scenario={scenario} budgetPence={budgetPence} />

      <footer>
        <p>
          Synthetic data only. No order, supplier commitment or external project record is
          changed. Durable runs, manager decisions and audit are local reference features;
          approval is recorded, while production identity and cloud deployment are not.
        </p>
      </footer>
    </main>
  );
}
