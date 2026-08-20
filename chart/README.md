# sre-agent Helm chart

This chart installs one SRE agent instance with cluster-wide read access. It
defaults to Anthropic, disables tracing, and does not grant writer permissions.

## OpenAI smoke test

Create a Secret without putting the API key in Helm values:

```bash
kubectl create namespace sre-agent
read -s "OPENAI_API_KEY?OpenAI API key: "
echo
kubectl -n sre-agent create secret generic sre-agent-credentials \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
unset OPENAI_API_KEY

helm upgrade --install sre-agent ./chart \
  --namespace sre-agent \
  --set image.tag=openai-test \
  --set config.llmProvider=openai \
  --set config.monitoringEnabled=false \
  --set existingSecret=sre-agent-credentials \
  --wait
```

Then run:

```bash
kubectl -n sre-agent port-forward svc/sre-agent 8080:80
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/api/trigger-check
helm test sre-agent --namespace sre-agent
```

## Anthropic

Use a Secret containing `ANTHROPIC_API_KEY`; Anthropic is already the default:

```bash
helm upgrade --install sre-agent ./chart \
  --namespace sre-agent --create-namespace \
  --set existingSecret=sre-agent-credentials \
  --wait
```

## Credentials

For production, set `existingSecret` to an externally managed Secret. Supported
keys are documented in `values.yaml`. Setting `secrets.create=true` is intended
only for development because those values are stored in Helm release state.

## Writer access

`rbac.write.create=false` is the safe default. Enabling it grants broad
cluster-wide create, patch, update, and delete permissions. Application-level
human approval remains in place, but the HTTP API currently needs an external
authentication layer before it should be exposed through an Ingress.

For reproducible deployments, set `image.digest=sha256:...`; it takes precedence
over `image.tag`.

Released charts default the image tag to `Chart.appVersion`. Local source-tree
testing must set an image tag that has already been published, such as
`image.tag=openai-test`.
