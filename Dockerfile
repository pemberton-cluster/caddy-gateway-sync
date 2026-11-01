FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# Dependencies are frozen in constraints.txt (transitive closure of
# kopf/kubernetes, pip freeze from a clean venv); the image is built once per
# chart release so the pod never talks to PyPI at runtime.
COPY charts/caddy-gateway-sync/operator/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir --constraint /tmp/constraints.txt \
      kopf==1.45.2 kubernetes==37.0.1 \
    && rm /tmp/constraints.txt

# Same non-root uid as the chart's securityContext (no passwd entry needed:
# the deployment sets USER=operator for kopf/getpass).
USER 65532:65532
