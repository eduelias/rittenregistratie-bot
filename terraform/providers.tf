terraform {
  required_version = ">= 1.5"
  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }
}

# Owner of the repository. Authenticate with a GITHUB_TOKEN that has repo admin
# scope (e.g. export GITHUB_TOKEN=...).
provider "github" {
  owner = var.owner
}

variable "owner" {
  type        = string
  default     = "eduelias"
  description = "GitHub account/organisation that owns the repository."
}

variable "repo_name" {
  type        = string
  default     = "rittenregistratie-bot"
  description = "Repository name."
}
