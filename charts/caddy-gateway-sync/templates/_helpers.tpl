{{/*
Expand the name of the chart.
*/}}
{{- define "caddy-gateway-sync.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "caddy-gateway-sync.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- printf "%s" $name }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "caddy-gateway-sync.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "caddy-gateway-sync.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "caddy-gateway-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "caddy-gateway-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "caddy-gateway-sync.serviceAccountName" -}}
{{- default (include "caddy-gateway-sync.fullname" .) .Values.serviceAccount.name }}
{{- end }}

{{/*
Name of the Secret holding the OPNsense API credentials.
*/}}
{{- define "caddy-gateway-sync.secretName" -}}
{{- if .Values.opnsense.existingSecret }}
{{- .Values.opnsense.existingSecret }}
{{- else }}
{{- include "caddy-gateway-sync.fullname" . }}
{{- end }}
{{- end }}
