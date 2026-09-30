#!/usr/bin/env python3
"""Test secret delivery inspection rules against compliant volume mounts and prohibited environment variables."""

from __future__ import annotations

import unittest

import yaml
from check_secret_delivery import IGNORE_SECRET_ENV_ANNOTATION, manifest_errors


class SecretDeliveryTest(unittest.TestCase):
    @staticmethod
    def test_file_mount_passes() -> None:
        manifest = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: test-ns
spec:
  template:
    spec:
      containers:
        - name: app
          image: test:1.0
          volumeMounts:
            - name: secret-vol
              mountPath: /secrets
      volumes:
        - name: secret-vol
          secret:
            secretName: db-creds
"""
        errors = manifest_errors(yaml.safe_load_all(manifest))
        assert errors == []

    @staticmethod
    def test_secret_key_ref_fails_without_annotation() -> None:
        manifest = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: test-ns
spec:
  template:
    spec:
      containers:
        - name: app
          image: test:1.0
          env:
            - name: DB_PASS
              valueFrom:
                secretKeyRef:
                  name: db-creds
                  key: password
"""
        errors = manifest_errors(yaml.safe_load_all(manifest))
        assert len(errors) == 1
        assert "DB_PASS" in errors[0]
        assert "db-creds" in errors[0]

    @staticmethod
    def test_secret_key_ref_passes_with_workload_annotation() -> None:
        manifest = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: test-ns
  annotations:
    {IGNORE_SECRET_ENV_ANNOTATION}: "Legacy upstream requirement"
spec:
  template:
    spec:
      containers:
        - name: app
          image: test:1.0
          env:
            - name: DB_PASS
              valueFrom:
                secretKeyRef:
                  name: db-creds
                  key: password
"""
        errors = manifest_errors(yaml.safe_load_all(manifest))
        assert errors == []

    @staticmethod
    def test_secret_key_ref_passes_with_pod_annotation() -> None:
        manifest = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: test-ns
spec:
  template:
    metadata:
      annotations:
        {IGNORE_SECRET_ENV_ANNOTATION}: "Legacy upstream requirement"
    spec:
      containers:
        - name: app
          image: test:1.0
          env:
            - name: DB_PASS
              valueFrom:
                secretKeyRef:
                  name: db-creds
                  key: password
"""
        errors = manifest_errors(yaml.safe_load_all(manifest))
        assert errors == []

    @staticmethod
    def test_env_from_secret_ref_fails_without_annotation() -> None:
        manifest = """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: test-stateful
  namespace: test-ns
spec:
  template:
    spec:
      containers:
        - name: worker
          image: test:1.0
          envFrom:
            - secretRef:
                name: shared-secret
"""
        errors = manifest_errors(yaml.safe_load_all(manifest))
        assert len(errors) == 1
        assert "shared-secret" in errors[0]

    @staticmethod
    def test_cronjob_fails_and_annotated_passes() -> None:
        manifest_fail = """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: test-cron
  namespace: test-ns
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: runner
              image: test:1.0
              env:
                - name: TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: api-token
                      key: token
"""
        errors = manifest_errors(yaml.safe_load_all(manifest_fail))
        assert len(errors) == 1

        manifest_pass = f"""
apiVersion: batch/v1
kind: CronJob
metadata:
  name: test-cron
  namespace: test-ns
  annotations:
    {IGNORE_SECRET_ENV_ANNOTATION}: "Upstream CLI tool requires env token"
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: runner
              image: test:1.0
              env:
                - name: TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: api-token
                      key: token
"""
        errors = manifest_errors(yaml.safe_load_all(manifest_pass))
        assert errors == []


if __name__ == "__main__":
    unittest.main()
