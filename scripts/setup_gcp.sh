#!/usr/bin/env bash
# One-off Google Cloud setup for the nightly sync. Run it once, as a user who may create
# projects in your Workspace organisation. The easiest place is Cloud Shell
# (https://shell.cloud.google.com), where gcloud is already installed and signed in.
#
#   PROJECT_ID=noc-contacts-sync GITHUB_REPO=JustinStafford/eventor-contacts-sync \
#     bash setup_gcp.sh
#
# It creates no keys. GitHub Actions proves its identity with a short-lived OIDC token
# (Workload Identity Federation); that identity may only ask one service account to sign
# a domain-wide-delegation assertion, and the Admin console (step printed at the end)
# limits that service account to the Contacts scope.
#
# Optional: LOCAL_USER=you@yourdomain lets that person run the sync from a laptop with
# `gcloud auth application-default login`.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID to a new or existing Google Cloud project ID}"
: "${GITHUB_REPO:?set GITHUB_REPO to owner/name of the private repository}"
SA_NAME="${SA_NAME:-eventor-contacts-sync}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating project $PROJECT_ID"
  gcloud projects create "$PROJECT_ID" --name="Eventor contacts sync"
fi
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

echo "Enabling APIs"
gcloud services enable people.googleapis.com iamcredentials.googleapis.com \
  iam.googleapis.com sts.googleapis.com --project "$PROJECT_ID"

if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating service account $SA_EMAIL"
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name="Eventor to Google Contacts sync"
fi

if ! gcloud iam workload-identity-pools describe "$POOL" --location=global \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating workload identity pool"
  gcloud iam workload-identity-pools create "$POOL" --location=global \
    --project "$PROJECT_ID" --display-name="GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER" --location=global \
  --workload-identity-pool="$POOL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating GitHub OIDC provider, restricted to $GITHUB_REPO"
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location=global \
    --workload-identity-pool="$POOL" --project "$PROJECT_ID" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'"
fi

PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}"
echo "Allowing $GITHUB_REPO to sign as the service account"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
  --role=roles/iam.serviceAccountTokenCreator --member="$PRINCIPAL" >/dev/null
if [ -n "${LOCAL_USER:-}" ]; then
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
    --role=roles/iam.serviceAccountTokenCreator --member="user:${LOCAL_USER}" >/dev/null
fi

CLIENT_ID="$(gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" --format='value(uniqueId)')"
cat <<SUMMARY

Done. Two manual steps remain.

1. Google Admin console (super admin): Security > Access and data control > API controls >
   Manage Domain Wide Delegation > Add new
     Client ID:    ${CLIENT_ID}
     OAuth scopes: https://www.googleapis.com/auth/contacts

2. GitHub repository ${GITHUB_REPO}: Settings > Secrets and variables > Actions
     Secret    EVENTOR_API_KEY                 = your club's Eventor API key
     Variable  GOOGLE_USER                     = the Workspace account whose contacts to sync
     Variable  GOOGLE_SERVICE_ACCOUNT          = ${SA_EMAIL}
     Variable  GCP_WORKLOAD_IDENTITY_PROVIDER  = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
SUMMARY
