terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # State is deliberately local (no S3/DynamoDB backend). This is a
  # single-operator demo environment; adding a remote backend would mean
  # creating an S3 bucket + lock table before the environment itself,
  # which is more AWS surface (and more cost) than this phase wants.
  #
  # Consequence to be aware of: terraform.tfstate lives on the machine that
  # ran `apply` and is gitignored. Lose it and Terraform loses track of
  # these resources (they keep running; Terraform just can't manage them).
  # Back it up if that matters. See docs/aws-deployment.md.
}

provider "aws" {
  region = var.aws_region

  # Every resource this stack creates carries these, so everything is
  # attributable and easy to find in Cost Explorer / the console. Nothing
  # outside this stack gets touched, because this configuration only ever
  # *creates* resources — it reads no pre-existing infrastructure except
  # the Canonical-published AMI id (see ec2.tf).
  default_tags {
    tags = {
      Project     = "Sentinel-SRE"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}
