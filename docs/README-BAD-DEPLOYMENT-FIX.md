# Bad Deployment Scenario - Fixed

The `bad-deployment` scenario now changes the **container image** instead of only changing an environment variable. It also uses Kubernetes `Recreate` for this demo so the old Pods are removed before the bad Pod is created.

The existing RCA code detects the image change and produces `0.96` confidence, which is above the existing `0.95` rollback threshold. **Do not lower the threshold.**

Run it the same way you normally run the scenario:

```bash
./scripts/incident-scenarios.sh bad-deployment
```

For the CI/smoke-test mode that automatically rolls back:

```bash
./scripts/incident-scenarios.sh bad-deployment citizen-portal true
```

After the autonomous rollback, you can inspect the image with:

```bash
kubectl get deployment citizen-service -n citizen-portal \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="citizen-service")].image}{"\n"}'
```
