# Public, MIT-licensed open-source core: a Belastingdienst-compliant WhatsApp
# trip logger with a plugin system. No AI, never fabricates or reclassifies
# trips.
resource "github_repository" "this" {
  name        = var.repo_name
  description = "Belastingdienst-compliant WhatsApp trip logger (rittenregistratie) with a plugin system."
  visibility  = "public"

  has_issues   = true
  has_wiki     = false
  has_projects = false

  allow_merge_commit = true
  allow_squash_merge = true
  allow_rebase_merge = true
  delete_branch_on_merge = true

  # Managed out of band; do not let TF clobber the license/gitignore choices.
  lifecycle {
    ignore_changes = [auto_init, gitignore_template, license_template]
  }
}

# Strict protection on main, with administrators allowed to bypass so the sole
# maintainer can still merge / push directly when necessary.
resource "github_branch_protection" "main" {
  repository_id = github_repository.this.node_id
  pattern       = "main"

  enforce_admins = false # administrators can bypass

  required_pull_request_reviews {
    required_approving_review_count = 1
    dismiss_stale_reviews           = true
  }

  required_status_checks {
    strict   = true
    contexts = ["test"] # the CI job name in .github/workflows/ci.yml
  }

  allows_force_pushes = false
  allows_deletions    = false
}

output "repository_url" {
  value = github_repository.this.html_url
}
