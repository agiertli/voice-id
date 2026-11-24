# Thin serve.py-compatible adapter in front of OpenVINO Model Server. No model weights inside.
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.6

RUN microdnf install -y --disablerepo="*" --enablerepo="ubi-9-baseos-rpms" --enablerepo="ubi-9-appstream-rpms" \
      python3.11 python3.11-pip && microdnf clean all

WORKDIR /app
COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY ovms_client.py app.py ./

# OpenShift restricted SCC: arbitrary UID, GID 0
RUN chgrp -R 0 /app && chmod -R g=u /app
USER 1001

ENV PYTHONUNBUFFERED=1 TMPDIR=/tmp
EXPOSE 8080
CMD ["python3.11", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "app:app"]
