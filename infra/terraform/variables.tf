variable "aws_profile" {
  description = "Optional named profile. Empty uses the standard AWS credential chain (including OIDC role credentials)."
  type        = string
  default     = ""
}

variable "expected_account_id" {
  description = "Expected AWS account ID; Terraform refuses to plan/apply against a different account."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a 12-digit account ID."
  }
}

variable "expected_stage_role_name" {
  description = "Expected assumed-role name for the current stage (bootstrap or deploy)."
  type        = string

  validation {
    condition     = can(regex("^focused-delivery-recovery-(bootstrap|deploy)$", var.expected_stage_role_name))
    error_message = "expected_stage_role_name must be the reviewed bootstrap or deploy role."
  }
}

variable "runtime_boundary_policy_json" {
  description = "Exact independently verified rendered runtime boundary policy JSON passed by the reviewed execution wrapper. The value is recorded in the saved plan so apply does not depend on a transient file."
  type        = string

  validation {
    condition     = length(var.runtime_boundary_policy_json) > 0 && can(jsondecode(var.runtime_boundary_policy_json))
    error_message = "runtime_boundary_policy_json must be a non-empty JSON document."
  }
}

variable "aws_region" {
  description = "Approved region for the reference deployment."
  type        = string
  default     = "eu-west-2"

  validation {
    condition     = var.aws_region == "eu-west-2"
    error_message = "The reviewed Phase 4 deployment is limited to eu-west-2."
  }
}

variable "project_tag" {
  description = "Project tag used for scoped inventory and cleanup."
  type        = string
  default     = "focused-delivery-recovery"
}

variable "owner_tag" {
  description = "Owner tag for the reviewed demo resources."
  type        = string
  default     = "koate"
}

variable "vpc_id" {
  description = "Existing VPC selected after read-only prerequisite checks."
  type        = string
}

variable "subnet_id" {
  description = "Existing public subnet with outbound 443/53 reachability. Empty selects the first default-VPC subnet for review only."
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "Single small ARM instance; default is 2 vCPU / 4 GiB."
  type        = string
  default     = "t4g.medium"

  validation {
    condition     = var.instance_type == "t4g.medium"
    error_message = "Keep the reviewed t4g.medium size unless the cost packet is revised."
  }
}

variable "root_volume_size_gb" {
  description = "Encrypted gp3 root volume."
  type        = number
  default     = 10
}

variable "data_volume_size_gb" {
  description = "Encrypted gp3 persistent application/checkpoint volume."
  type        = number
  default     = 20
}

variable "data_device_name" {
  description = "EBS attachment device name; Linux maps it to an NVMe device at boot."
  type        = string
  default     = "/dev/sdf"
}

variable "image_uri" {
  description = "Fully pinned ECR image URI including @sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_uri))
    error_message = "image_uri must be pinned by @sha256 digest; tags alone are not accepted."
  }
}

variable "ecr_repository_name" {
  description = "Project-owned ECR repository."
  type        = string
  default     = "focused-delivery-recovery"
}

variable "log_group_name" {
  description = "Project-owned CloudWatch log group."
  type        = string
  default     = "/aws/delivery-recovery/app"
}

variable "log_retention_days" {
  description = "Short retention to bound residual log cost."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30], var.log_retention_days)
    error_message = "Use a supported short retention period."
  }
}

variable "github_oidc_subject" {
  description = "Exact GitHub OIDC subject allowed to assume the proposed deploy role."
  type        = string
  default     = "repo:koatedevopskpai/multi-agent-optimizer:ref:refs/heads/feature/focused-delivery-recovery"
}
