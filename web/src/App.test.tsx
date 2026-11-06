import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const utilityEvaluation = {
  baseline: {
    finish_offset_workdays: 15,
    elapsed_workdays: 15,
    critical_task_ids: ["A", "C", "D", "E", "G"],
  },
  disrupted: {
    finish_offset_workdays: 17,
    elapsed_workdays: 17,
    critical_task_ids: ["A", "C", "D", "E", "G"],
  },
  candidates: [
    {
      candidate_id: "candidate-expedite",
      option_ids: ["EXPEDITE_PANEL"],
      task_reductions: { C: 2 },
      project_saving_workdays: 2,
      finish_offset_workdays: 15,
      cost_pence: 180_000,
      feasible: true,
      rejection_codes: [],
    },
  ],
  recommended_candidate_id: "candidate-expedite",
  ranked_feasible_candidate_ids: ["candidate-expedite"],
};

function response(body: unknown) {
  return { ok: true, json: async () => body };
}

describe("App result freshness", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("clears results and marks inputs stale after a budget change", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response(utilityEvaluation) as Response);
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: /evaluate recovery/i }));
    expect(await screen.findByText("Recommendation")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();

    const budget = screen.getByLabelText(/recovery budget/i);
    await user.clear(budget);
    await user.type(budget, "1000");

    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("Recommendation")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/inputs changed/i);
  });

  it("ignores a superseded response after switching scenario", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    const oldResponse = new Promise((resolve) => {
      resolveOld = resolve;
    });
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockReturnValueOnce(oldResponse as Promise<Response>);
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: /evaluate recovery/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await user.selectOptions(screen.getByLabelText(/delivery scenario/i), "parallel");
    await act(async () => {
      resolveOld?.(response(utilityEvaluation));
      await Promise.resolve();
    });

    expect(screen.getByLabelText(/delivery scenario/i)).toHaveValue("parallel");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("Recommendation")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
