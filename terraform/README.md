# Infrastructure as documentation

This directory documents how the GitHub repository and its branch protection are
configured, using the Terraform GitHub provider.

> **Note**
> The repository was originally created with the GitHub CLI (`gh`). This
> Terraform configuration is kept as living documentation / reference and can be
> used to reproduce the setup. It uses local state and is **not** applied
> automatically by CI. To adopt it for real management, run `terraform import`
> for the existing resources first.

## Usage

```bash
export GITHUB_TOKEN=...   # a token with repo admin scope
terraform init
terraform plan
# terraform import github_repository.this rittenregistratie-bot   # if adopting
# terraform apply
```

## What it describes

- `github_repository.this` — the repository, visibility and settings.
- `github_branch_protection.main` — strict protection on `main`:
  required PR review, required CI status check, no force-push / deletion,
  with administrators allowed to bypass (`enforce_admins = false`).
