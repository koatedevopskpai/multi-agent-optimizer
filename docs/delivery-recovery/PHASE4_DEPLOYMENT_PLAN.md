# AWS reference deployment plan

The historical AWS reference and its limits are described in the current [architecture](../portfolio/ARCHITECTURE.md). Project billable resources were removed on 5 November2026; there is no active deployment to connect to.

Source templates are in `infra/terraform`, `infra/bootstrap` and `infra/iam`. Account-specific rendered inputs, temporary credentials, saved plans, raw state and execution receipts remain private. New deployment requires a fresh account/region/subnet decision, current image scan, exact IAM review, identity verification, saved plan review, bounded budget/window and teardown plan. Local demo startup uses the root README and creates no AWS resources.

This is an adapted publication reference. It is not approval to execute the historical commands or proof of AWS recreation, production readiness or host-loss recovery.
