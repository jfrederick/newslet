# Auto-deploy on merge to main

`.github/workflows/ci.yml` runs `pytest` + `ruff` on every PR and, on
push to `main`, runs `sam deploy` against your AWS account using OIDC
(no long-lived access keys stored anywhere).

This is a one-time AWS + GitHub setup. After this is done, every merge
to `main` ships to AWS automatically.

## 1. Create the GitHub OIDC provider in your AWS account

Skip this step if you've already wired GitHub Actions to this AWS
account for another repo.

In the AWS console:

1. **IAM → Identity providers → Add provider**
2. Provider type: **OpenID Connect**
3. Provider URL: `https://token.actions.githubusercontent.com`
4. Audience: `sts.amazonaws.com`
5. Add provider.

Or via the CLI:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

## 2. Create the IAM role GitHub Actions will assume

Save your AWS account ID (`aws sts get-caller-identity --query Account
--output text`) and your repo slug (e.g. `jfrederick/newslet`).

Save the following as `trust-policy.json`, replacing both
placeholders:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:<OWNER>/<REPO>:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

The `sub` condition restricts the role to **pushes to the `main`
branch of this exact repo** — a PR from a fork or a different branch
cannot assume it.

Create the role and attach a permission policy:

```bash
aws iam create-role \
  --role-name newslet-deployer \
  --assume-role-policy-document file://trust-policy.json

# Permissive but acceptable for a personal app you control end-to-end.
# Scope this down later if you ever want to (CloudFormation +
# Lambda + DynamoDB + API Gateway + EventBridge + SSM-read + S3 for
# the SAM artifact bucket + IAM PassRole are the actual surfaces
# touched).
aws iam attach-role-policy \
  --role-name newslet-deployer \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
```

Note the role ARN:

```bash
aws iam get-role --role-name newslet-deployer --query 'Role.Arn' --output text
# arn:aws:iam::<ACCOUNT_ID>:role/newslet-deployer
```

## 3. Add the role ARN to GitHub secrets

On the repo: **Settings → Secrets and variables → Actions → New
repository secret**.

- Name: `AWS_ROLE_ARN`
- Value: the role ARN from step 2.

## 4. Test it

Make a tiny change on a branch, open a PR — the `test` job runs,
nothing deploys. Merge to `main` — the `test` job runs again, then
`deploy` runs `sam deploy` and the changes hit AWS. Watch the run in
the **Actions** tab.

A successful deploy ends with CloudFormation either reporting "No
changes to deploy" (for code-only edits with the same template) or
"Successfully created/updated stack". Either is fine.

## Tearing it down

```bash
aws iam detach-role-policy --role-name newslet-deployer \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam delete-role --role-name newslet-deployer
# Optionally remove the OIDC provider if no other repos use it:
# aws iam delete-open-id-connect-provider --open-id-connect-provider-arn ...
```

And delete the `AWS_ROLE_ARN` secret on the GitHub side.
