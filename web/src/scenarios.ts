export type TaskInput = {
  id: string;
  name: string;
  duration_workdays: number;
  dependency_ids: string[];
};

export type TaskDurationChange = {
  task_id: string;
  new_duration_workdays: number;
};

export type RecoveryOption = {
  option_id: string;
  changes: TaskDurationChange[];
  incremental_cost_pence: number;
  incompatible_option_ids?: string[];
};

export type ProjectInput = {
  schema_version: "delivery-recovery-v1";
  project_id: string;
  version: number;
  time_unit: "working_day";
  start_offset_workdays: number;
  target_finish_offset_workdays: number;
  budget_pence: number;
  currency: "GBP";
  tasks: TaskInput[];
  catalogue: RecoveryOption[];
};

export type Disruption = {
  task_id: string;
  revised_duration_workdays: number;
};

export type Scenario = {
  id: string;
  label: string;
  description: string;
  budgetEditable: boolean;
  project: ProjectInput;
  disruption: Disruption | null;
};

export const UTILITY_SCENARIO: Scenario = {
  id: "utility",
  label: "Utilities panel delay",
  description:
    "A supplier delay moves the synthetic pump-control installation from day 15 to day 17. Compare permitted recovery options against a GBP 2,000 budget.",
  budgetEditable: true,
  project: {
    schema_version: "delivery-recovery-v1",
    project_id: "UTILITY-DEMO-001",
    version: 1,
    time_unit: "working_day",
    start_offset_workdays: 0,
    target_finish_offset_workdays: 15,
    budget_pence: 200_000,
    currency: "GBP",
    tasks: [
      {
        id: "A",
        name: "Access and work-pack preparation",
        duration_workdays: 2,
        dependency_ids: [],
      },
      { id: "B", name: "Site survey", duration_workdays: 3, dependency_ids: ["A"] },
      { id: "C", name: "Control-panel supply", duration_workdays: 5, dependency_ids: ["A"] },
      { id: "D", name: "Installation", duration_workdays: 4, dependency_ids: ["B", "C"] },
      { id: "E", name: "Commissioning checks", duration_workdays: 3, dependency_ids: ["D"] },
      {
        id: "F",
        name: "Operator training preparation",
        duration_workdays: 2,
        dependency_ids: ["B"],
      },
      { id: "G", name: "Handover", duration_workdays: 1, dependency_ids: ["E", "F"] },
    ],
    catalogue: [
      {
        option_id: "EXPEDITE_PANEL",
        changes: [{ task_id: "C", new_duration_workdays: 5 }],
        incremental_cost_pence: 180_000,
      },
      {
        option_id: "INSTALL_SUPPORT",
        changes: [{ task_id: "D", new_duration_workdays: 3 }],
        incremental_cost_pence: 120_000,
      },
    ],
  },
  disruption: { task_id: "C", revised_duration_workdays: 7 },
};

export const PARALLEL_SCENARIO: Scenario = {
  id: "parallel",
  label: "Parallel critical branches",
  description:
    "Two equal critical branches. Shortening one task does not shorten the project; shortening both does.",
  budgetEditable: false,
  project: {
    schema_version: "delivery-recovery-v1",
    project_id: "PARALLEL-DEMO-001",
    version: 1,
    time_unit: "working_day",
    start_offset_workdays: 0,
    target_finish_offset_workdays: 5,
    budget_pence: 10_000,
    currency: "GBP",
    tasks: [
      { id: "S", name: "Start", duration_workdays: 1, dependency_ids: [] },
      { id: "P", name: "Branch P", duration_workdays: 3, dependency_ids: ["S"] },
      { id: "Q", name: "Branch Q", duration_workdays: 3, dependency_ids: ["S"] },
      { id: "E", name: "End", duration_workdays: 1, dependency_ids: ["P", "Q"] },
    ],
    catalogue: [
      {
        option_id: "SHORTEN_P",
        changes: [{ task_id: "P", new_duration_workdays: 2 }],
        incremental_cost_pence: 500,
      },
      {
        option_id: "SHORTEN_Q",
        changes: [{ task_id: "Q", new_duration_workdays: 2 }],
        incremental_cost_pence: 500,
      },
    ],
  },
  disruption: null,
};

export const SCENARIOS: Scenario[] = [UTILITY_SCENARIO, PARALLEL_SCENARIO];
