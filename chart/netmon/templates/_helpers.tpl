{{- define "netmon.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "netmon.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/part-of: netmon
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* usage: include "netmon.selector" (list . "probe") */}}
{{- define "netmon.selector" -}}
app.kubernetes.io/name: {{ index . 1 }}
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
{{- end -}}

{{- define "netmon.prometheusUrl" -}}
{{- if .Values.prometheus.enabled -}}
http://{{ include "netmon.fullname" . }}-prometheus:{{ .Values.prometheus.service.port }}
{{- else -}}
{{- required "grafana.externalPrometheusUrl is required when prometheus.enabled=false" .Values.grafana.externalPrometheusUrl -}}
{{- end -}}
{{- end -}}

{{/* PVC: include "netmon.pvc" (dict "ctx" . "name" "xxx" "p" .Values.xxx.persistence) */}}
{{- define "netmon.pvc" -}}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "netmon.fullname" .ctx }}-{{ .name }}
  labels:
    {{- include "netmon.labels" .ctx | nindent 4 }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  accessModes: [ReadWriteOnce]
  {{- with .p.storageClass }}
  storageClassName: {{ . }}
  {{- end }}
  resources:
    requests:
      storage: {{ .p.size }}
{{- end -}}

{{- define "netmon.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "netmon.fullname" .ctx }}-{{ .name }}
  labels:
    {{- include "netmon.labels" .ctx | nindent 4 }}
    {{- include "netmon.selector" (list .ctx .name) | nindent 4 }}
spec:
  type: {{ .s.type }}
  selector:
    {{- include "netmon.selector" (list .ctx .name) | nindent 4 }}
  ports:
    - name: http
      port: {{ .s.port }}
      targetPort: http
      {{- if and (eq .s.type "NodePort") .s.nodePort }}
      nodePort: {{ .s.nodePort }}
      {{- end }}
{{- end -}}
