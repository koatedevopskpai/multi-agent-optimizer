terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

data "archive_file" "bootstrap" {
  type        = "zip"
  source_dir  = "${path.module}/../bootstrap"
  output_path = "${path.module}/.bootstrap.zip"
}

provider "aws" {
  region              = var.aws_region
  profile             = var.aws_profile != "" ? var.aws_profile : null
  allowed_account_ids = [var.expected_account_id]

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_vpc" "selected" {
  id = var.vpc_id
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

data "aws_subnet" "selected" {
  id = local.selected_subnet
}

data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  selected_subnet = var.subnet_id != "" ? var.subnet_id : data.aws_subnets.selected.ids[0]
  tags = {
    Project     = var.project_tag
    Environment = "reference"
    ManagedBy   = "terraform"
    Owner       = var.owner_tag
    Purpose     = "phase4-operational-evidence"
  }
}
