export type Facts = {
  finish_offset_workdays: number;
  elapsed_workdays: number;
  critical_task_ids: string[];
};

export type Candidate = {
  candidate_id: string;
  option_ids: string[];
  task_reductions: Record<string, number>;
  project_saving_workdays: number;
  finish_offset_workdays: number;
  cost_pence: number;
  feasible: boolean;
  rejection_codes: string[];
};

export type Evaluation = {
  baseline: Facts;
  disrupted: Facts;
  candidates: Candidate[];
  recommended_candidate_id: string | null;
  ranked_feasible_candidate_ids: string[];
};

export type PendingDecision = {
  kind: "approval";
  run_id: string;
  candidate_id: string;
  project_version: number;
  input_hash: string;
  catalogue_hash: string;
  finish_offset_workdays: number | null;
  cost_pence: number | null;
};

export type PendingQuestion = {
  field: string;
  question: string;
};

export type ProviderOutcome = {
  mode: string;
  reason_code: string | null;
  transport_attempts: number;
  workflow_input_tokens: number;
  workflow_output_tokens: number;
  usage_source: string;
};

export type RunView = {
  run_id: string;
  thread_id: string;
  status:
    | "queued"
    | "running"
    | "needs_input"
    | "awaiting_approval"
    | "approved"
    | "rejected"
    | "completed"
    | "degraded"
    | "failed"
    | "expired";
  project_id: string;
  project_version: number;
  created_by: string;
  revision: number;
  decision_status: string | null;
  evaluation: Evaluation | null;
  explanation: {
    recommended_candidate_id: string | null;
    summary: string;
    finish_offset_workdays: number | null;
    cost_pence: number | null;
  } | null;
  provider_outcome: ProviderOutcome | null;
  pending_question: PendingQuestion | null;
  pending_decision: PendingDecision | null;
  error: { code: string; message: string } | null;
  created_at: string;
  updated_at: string;
  retention_expires_at: string | null;
  access: { actor_id: string; roles: string[] };
};

export type AuditEvent = {
  event_id: string;
  event_type: string;
  actor_id: string | null;
  actor_role: string | null;
  decision_id: string | null;
  revision_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export type Revision = {
  revision_id: string;
  revision: number;
  project_version: number;
  candidate_id: string;
  plan: Record<string, unknown>;
  decision_id: string;
  created_at: string;
};

export type AuditResponse = {
  run_id: string;
  events: AuditEvent[];
  revisions: Revision[];
};

export type CreateRunResponse = {
  run_id: string;
  thread_id: string;
  status: string;
  status_url: string;
  idempotent_replay: boolean;
};
