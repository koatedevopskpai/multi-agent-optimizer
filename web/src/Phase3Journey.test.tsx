import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function reply(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

const evaluation = {
  baseline: {
    finish_offset_workdays: 15,
    elapsed_workdays: 15,
    critical_task_ids: ["A", "C"],
  },
  disrupted: {
    finish_offset_workdays: 17,
    elapsed_workdays: 17,
    critical_task_ids: ["A", "C"],
  },
  candidates: [
    {
      candidate_id: "CAND-1",
      option_ids: ["EXPEDITE_PANEL"],
      task_reductions: { C: 2 },
      project_saving_workdays: 2,
      finish_offset_workdays: 15,
      cost_pence: 180_000,
      feasible: true,
      rejection_codes: [],
    },
  ],
  recommended_candidate_id: "CAND-1",
  ranked_feasible_candidate_ids: ["CAND-1"],
};

const binding = {
  kind: "approval",
  run_id: "run-1",
  candidate_id: "CAND-1",
  project_version: 1,
  input_hash: "input-hash",
  catalogue_hash: "catalogue-hash",
  finish_offset_workdays: 15,
  cost_pence: 180_000,
};

function runView(status: string, overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-1",
    thread_id: "thread-1",
    status,
    project_id: "UTILITY-DEMO-001",
    project_version: 1,
    created_by: "planner.demo",
    revision: 0,
    decision_status: null,
    evaluation,
    explanation: {
      recommended_candidate_id: "CAND-1",
      summary: "Expedite the panel.",
      finish_offset_workdays: 15,
      cost_pence: 180_000,
    },
    provider_outcome: {
      mode: "model",
      reason_code: null,
      transport_attempts: 1,
      workflow_input_tokens: 10,
      workflow_output_tokens: 5,
      usage_source: "reported",
    },
    pending_question: null,
    pending_decision: status === "awaiting_approval" ? binding : null,
    error: null,
    created_at: "2026-11-02T18:00:00Z",
    updated_at: "2026-11-02T18:00:01Z",
    retention_expires_at: null,
    access: { actor_id: "planner.demo", roles: ["planner"] },
    ...overrides,
  };
}

describe("Phase 3 durable journey", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("starts a run, shows authoritative facts and commits one approval", async () => {
    let decided = false;
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/v1/runs" && method === "POST") {
        return reply({
          run_id: "run-1",
          thread_id: "thread-1",
          status: "queued",
          status_url: "/v1/runs/run-1",
          idempotent_replay: false,
        }, 202) as Response;
      }
      if (url === "/v1/runs/run-1" && method === "GET") {
        return reply(
          decided
            ? runView("completed", { revision: 1, decision_status: "approved" })
            : runView("awaiting_approval"),
        ) as Response;
      }
      if (url === "/v1/runs/run-1/decisions") {
        decided = true;
        return reply({
          decision: { decision_id: "decision-1", approved: 1 },
          revision: { revision_id: "revision-1", revision: 1 },
          idempotent_replay: false,
        }) as Response;
      }
      if (url === "/v1/runs/run-1/audit") {
        if (!decided) {
          return reply({
            run_id: "run-1",
            events: [
              {
                event_id: "event-pending",
                event_type: "RUN_WAITING",
                actor_id: "planner.demo",
                actor_role: "planner",
                decision_id: null,
                revision_id: null,
                payload: {},
                created_at: "2026-11-02T18:00:01Z",
              },
            ],
            revisions: [],
          }) as Response;
        }
        return reply({
          run_id: "run-1",
          events: [
            {
              event_id: "event-1",
              event_type: "DECISION_APPROVED",
              actor_id: "manager.demo",
              actor_role: "manager",
              decision_id: "decision-1",
              revision_id: "revision-1",
              payload: {},
              created_at: "2026-11-02T18:00:02Z",
            },
          ],
          revisions: [
            {
              revision_id: "revision-1",
              revision: 1,
              project_version: 2,
              candidate_id: "CAND-1",
              plan: {},
              decision_id: "decision-1",
              created_at: "2026-11-02T18:00:02Z",
            },
          ],
        }) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    await user.type(screen.getByLabelText(/manager bearer token/i), "manager-token");
    await user.click(screen.getByRole("button", { name: /start durable run/i }));

    expect(
      await screen.findByRole("heading", { name: /awaiting manager decision/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Day 17/)).toBeInTheDocument();
    const approval = screen.getByRole("heading", {
      name: /awaiting manager decision/i,
    }).parentElement;
    expect(approval).not.toBeNull();
    expect(within(approval as HTMLElement).getByText(/£1,800\.00/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /approve recovery/i }));
    await waitFor(() =>
      expect(screen.getByText(/1 committed revision/i)).toBeInTheDocument(),
    );
    const decisionCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url) === "/v1/runs/run-1/decisions" && init?.method === "POST",
    );
    expect(decisionCall).toBeDefined();
    expect((decisionCall?.[1]?.headers as Record<string, string>)[
      "Idempotency-Key"
    ]).toBe("demo-decision-1");
  });

  it("shows a clarification form and keeps the run identity", async () => {
    let clarified = false;
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/v1/runs" && method === "POST") {
        return reply({
          run_id: "run-1",
          thread_id: "thread-1",
          status: "queued",
          status_url: "/v1/runs/run-1",
          idempotent_replay: false,
        }, 202) as Response;
      }
      if (url === "/v1/runs/run-1" && method === "GET") {
        return reply(
          clarified
            ? runView("awaiting_approval")
            : runView("needs_input", {
                pending_question: {
                  field: "budget_pence",
                  question: "What recovery budget is available?",
                },
                pending_decision: null,
              }),
        ) as Response;
      }
      if (url === "/v1/runs/run-1/clarifications") {
        clarified = true;
        return reply({ run_id: "run-1", status: "queued" }, 202) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    await user.click(screen.getByRole("button", { name: /start durable run/i }));
    expect(
      await screen.findByText(/what recovery budget is available/i),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText(/^answer$/i), "200000");
    await user.click(screen.getByRole("button", { name: /submit clarification/i }));
    expect(
      await screen.findByRole("heading", { name: /awaiting manager decision/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("thread-1")).toBeInTheDocument();
  });

  it("surfaces a stale decision conflict without claiming a revision", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/v1/runs" && method === "POST") {
        return reply({
          run_id: "run-1",
          thread_id: "thread-1",
          status: "queued",
          status_url: "/v1/runs/run-1",
          idempotent_replay: false,
        }, 202) as Response;
      }
      if (url === "/v1/runs/run-1" && method === "GET") {
        return reply(runView("awaiting_approval")) as Response;
      }
      if (url === "/v1/runs/run-1/decisions") {
        return reply(
          {
            error: {
              code: "STALE_DECISION",
              message: "The decision does not match the current project.",
            },
          },
          409,
        ) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    await user.type(screen.getByLabelText(/manager bearer token/i), "manager-token");
    await user.click(screen.getByRole("button", { name: /start durable run/i }));
    await screen.findByRole("heading", { name: /awaiting manager decision/i });
    await user.click(
      screen.getByRole("button", { name: /attempt stale decision \(demo\)/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/STALE_DECISION/);
    const staleCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url) === "/v1/runs/run-1/decisions" && init?.method === "POST",
    );
    expect(JSON.parse(String(staleCall?.[1]?.body)).project_version).toBe(2);
    expect(screen.queryByText(/1 committed revision/i)).not.toBeInTheDocument();
  });
});


describe("Phase 3 run-bound history", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  function audit(runId: string, event: string, revisionCount = 0) {
    return {
      run_id: runId,
      events: [
        {
          event_id: `${runId}-event`,
          event_type: event,
          actor_id: "manager.demo",
          actor_role: "manager",
          decision_id: revisionCount ? "decision-1" : null,
          revision_id: revisionCount ? "revision-1" : null,
          payload: {},
          created_at: "2026-11-02T18:00:02Z",
        },
      ],
      revisions: revisionCount
        ? [
            {
              revision_id: "revision-1",
              revision: 1,
              project_version: 2,
              candidate_id: "CAND-1",
              plan: {},
              decision_id: "decision-1",
              created_at: "2026-11-02T18:00:02Z",
            },
          ]
        : [],
    };
  }

  it("clears completed history when switching to a pending run and shows approval", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url === "/v1/runs/run-1") {
        return reply(
          runView("completed", {
            run_id: "run-1",
            thread_id: "run-1",
            revision: 1,
            decision_status: "approved",
            pending_decision: null,
          }),
        ) as Response;
      }
      if (method === "GET" && url === "/v1/runs/run-2") {
        return reply(
          runView("awaiting_approval", {
            run_id: "run-2",
            thread_id: "run-2",
            pending_decision: { ...binding, run_id: "run-2" },
          }),
        ) as Response;
      }
      if (url === "/v1/runs/run-1/audit") {
        return reply(audit("run-1", "DECISION_APPROVED", 1)) as Response;
      }
      if (url === "/v1/runs/run-2/audit") {
        return reply(audit("run-2", "RUN_WAITING", 0)) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    const load = screen.getByLabelText(/load an existing run id/i);
    await user.type(load, "run-1");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    expect(await screen.findByText(/DECISION_APPROVED/)).toBeInTheDocument();
    expect(screen.getByText(/1 committed revision/i)).toBeInTheDocument();
    expect(screen.getByText(/approved by an authorised manager/i)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /pre-decision recommendation/i }),
    ).toBeInTheDocument();

    await user.clear(load);
    await user.type(load, "run-2");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    expect(await screen.findByText(/RUN_WAITING/)).toBeInTheDocument();
    expect(screen.queryByText(/DECISION_APPROVED/)).not.toBeInTheDocument();
    expect(screen.queryByText(/1 committed revision/i)).not.toBeInTheDocument();
  });

  it("ignores a delayed audit response from a previous run", async () => {
    let resolveAudit: ((value: unknown) => void) | undefined;
    const delayedAudit = new Promise((resolve) => {
      resolveAudit = resolve;
    });
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url === "/v1/runs/run-1") {
        return reply(runView("completed", { run_id: "run-1", thread_id: "run-1" })) as Response;
      }
      if (method === "GET" && url === "/v1/runs/run-2") {
        return reply(
          runView("awaiting_approval", {
            run_id: "run-2",
            thread_id: "run-2",
            pending_decision: { ...binding, run_id: "run-2" },
          }),
        ) as Response;
      }
      if (url === "/v1/runs/run-1/audit") {
        return delayedAudit as Promise<Response>;
      }
      if (url === "/v1/runs/run-2/audit") {
        return reply(audit("run-2", "RUN_WAITING", 0)) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    const load = screen.getByLabelText(/load an existing run id/i);
    await user.type(load, "run-1");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    await screen.findByText(/completed/i);
    await user.clear(load);
    await user.type(load, "run-2");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    await screen.findByText(/RUN_WAITING/);
    resolveAudit?.(reply(audit("run-1", "DECISION_APPROVED", 1)));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByText(/DECISION_APPROVED/)).not.toBeInTheDocument();
  });

  it("clears old history when a load fails", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url === "/v1/runs/run-1") {
        return reply(runView("completed", { run_id: "run-1", thread_id: "run-1" })) as Response;
      }
      if (url === "/v1/runs/run-1/audit") {
        return reply(audit("run-1", "DECISION_APPROVED", 1)) as Response;
      }
      if (method === "GET" && url === "/v1/runs/missing") {
        return reply({ error: { code: "RUN_NOT_FOUND", message: "missing" } }, 404) as Response;
      }
      throw new Error(`unexpected request ${method} ${url}`);
    });

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText(/planner bearer token/i), "planner-token");
    const load = screen.getByLabelText(/load an existing run id/i);
    await user.type(load, "run-1");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    await screen.findByText(/DECISION_APPROVED/);
    await user.clear(load);
    await user.type(load, "missing");
    await user.click(screen.getByRole("button", { name: /load run/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/RUN_NOT_FOUND/);
    expect(screen.queryByText(/DECISION_APPROVED/)).not.toBeInTheDocument();
    expect(screen.queryByText(/revision and audit history/i)).not.toBeInTheDocument();
  });
});
