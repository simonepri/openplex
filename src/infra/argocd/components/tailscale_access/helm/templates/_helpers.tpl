{{- /* Validates exact rendering mode and its non-secret registration inputs. */ -}}

{{- define "tailscale-access.validate" -}}
{{- if eq .Values.mode "disabled" -}}
{{- fail "tailscale-access must be rendered with cloud-secret, cloud-connector, or local-router mode" -}}
{{- end -}}
{{- $_ := required "clusterName is required" .Values.clusterName -}}
{{- $_ = required "serviceCIDR is required" .Values.serviceCIDR -}}
{{- if hasPrefix "cloud-" .Values.mode -}}
{{- $_ = required "internalDomain is required for cloud split DNS" .Values.internalDomain -}}
{{- $_ = required "cloud.oauthRemoteKey is required" .Values.cloud.oauthRemoteKey -}}
{{- if eq .Values.mode "cloud-connector" -}}
{{- if eq (len .Values.resolverRoutes) 0 -}}
{{- fail "cloud Connector mode requires provider resolver /32 routes" -}}
{{- end -}}
{{- $_ = include "tailscale-access.connectorHostnamePrefix" . -}}
{{- end -}}
{{- else if eq .Values.mode "local-router" -}}
{{- if gt (len .Values.resolverRoutes) 0 -}}
{{- fail "local router mode reaches CoreDNS through serviceCIDR and must not advertise provider resolver routes" -}}
{{- end -}}
{{- $_ = required "local.recordName is required" .Values.local.recordName -}}
{{- end -}}
{{- if .Values.egress.enabled -}}
{{- if eq (ne .Values.egress.gatewayIPv4 "") (gt (len .Values.egress.kubernetesAPIs) 0) -}}
{{- fail "private egress requires exactly one active-control gateway or managed Kubernetes API map" -}}
{{- end -}}
{{- range $clusterName, $transport := .Values.egress.kubernetesAPIs -}}
{{- if ne $transport.serviceName (printf "svc:kube-api-%s" $clusterName) -}}
{{- fail (printf "egress.kubernetesAPIs.%s.serviceName must match its cluster" $clusterName) -}}
{{- end -}}
{{- $_ = required (printf "egress.kubernetesAPIs.%s.ipv4 is required" $clusterName) $transport.ipv4 -}}
{{- end -}}
{{- else if or (ne .Values.egress.gatewayIPv4 "") (gt (len .Values.egress.kubernetesAPIs) 0) -}}
{{- fail "disabled private egress must not carry a gateway or managed Kubernetes APIs" -}}
{{- end -}}
{{- end -}}

{{/* Derive the shared prefix used by each highly available Connector replica. */}}
{{- define "tailscale-access.connectorHostnamePrefix" -}}
{{- $prefix := printf "%s-services" .Values.clusterName -}}
{{- if gt (len $prefix) 62 -}}
{{- fail "Connector hostnamePrefix must contain at most 62 characters" -}}
{{- end -}}
{{- $prefix -}}
{{- end -}}
