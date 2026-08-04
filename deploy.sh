#!/usr/bin/env bash
# deploy.sh — build, push to ECR, and deploy the SRE bot to EKS
set -euo pipefail

# Target account, region, and cluster come from the environment so no
# account-specific identifiers live in this public repo. Export them once, e.g.
#   export AWS_ACCOUNT=123456789012 AWS_REGION=us-east-2 CLUSTER=my-cluster
AWS_ACCOUNT="${AWS_ACCOUNT:?set AWS_ACCOUNT, e.g. export AWS_ACCOUNT=123456789012}"
AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER:?set CLUSTER, e.g. export CLUSTER=my-eks-cluster}"
ECR_REPO="${ECR_REPO:-sre-agent}"
IMAGE="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
TAG="${1:-latest}"
NAMESPACE="${NAMESPACE:-sre-agent}"

echo "==> Checking AWS auth..."
aws sts get-caller-identity --query 'Account' --output text > /dev/null

echo "==> Creating ECR repository (if not exists)..."
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" > /dev/null 2>&1 || \
  aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --output text > /dev/null

echo "==> Logging into ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Building image (${IMAGE}:${TAG})..."
docker build \
  --platform linux/amd64 \
  -t "${IMAGE}:${TAG}" \
  -t "${IMAGE}:$(git rev-parse --short HEAD 2>/dev/null || echo 'local')" \
  "$(dirname "$0")"

echo "==> Pushing image..."
docker push "${IMAGE}:${TAG}"
docker push "${IMAGE}:$(git rev-parse --short HEAD 2>/dev/null || echo 'local')" 2>/dev/null || true

echo "==> Ensuring kubectl context is set to ${CLUSTER}..."
aws eks update-kubeconfig --name "${CLUSTER}" --region "${AWS_REGION}"

echo "==> Checking secrets are populated..."
K8S_DIR="$(dirname "$0")/k8s"
if grep -q "REPLACE_WITH_BASE64" "${K8S_DIR}/secret.yaml"; then
  echo ""
  echo "ERROR: k8s/secret.yaml still contains placeholder values."
  echo "Run the following to encode your keys, then paste into k8s/secret.yaml:"
  echo ""
  echo "  echo -n 'sk-ant-...'   | base64   # ANTHROPIC_API_KEY"
  echo "  echo -n 'lsv2_pt_...' | base64   # LANGSMITH_API_KEY"
  echo "  echo -n 'xoxb-...'    | base64   # SLACK_BOT_TOKEN"
  echo "  echo -n 'xapp-...'    | base64   # SLACK_APP_TOKEN"
  echo ""
  exit 1
fi

echo "==> Applying Kubernetes manifests..."
kubectl apply -k "${K8S_DIR}"

# deployment.yaml ships a placeholder image so no account-specific registry is
# committed. Point it at the real one here, before waiting on the rollout, so
# the placeholder is never actually pulled.
echo "==> Setting image to ${IMAGE}:${TAG}..."
kubectl set image deployment/sre-agent \
  "sre-agent=${IMAGE}:${TAG}" -n "${NAMESPACE}"

echo "==> Waiting for rollout..."
kubectl rollout status deployment/sre-agent -n "${NAMESPACE}" --timeout=120s

echo "==> Deployment status:"
kubectl get pods -n "${NAMESPACE}"

echo ""
echo "Done! To access the web UI:"
echo "  kubectl port-forward svc/sre-agent 8080:80 -n ${NAMESPACE}"
echo "  open http://localhost:8080"
