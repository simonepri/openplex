# syntax=docker/dockerfile:1
# Builds the Coder snapshot portal web server and catalog browser image.

ARG BUILDER_IMAGE=golang:1.27-alpine
FROM ${BUILDER_IMAGE} AS builder

WORKDIR /src

COPY go.mod ./
COPY . .

# Build statically linked binary with pure Go standard library
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/coder-snapshot-portal ./cmd/portal

FROM gcr.io/distroless/static-debian13:nonroot

WORKDIR /
COPY --from=builder /out/coder-snapshot-portal /ko-app/coder-snapshot-portal

USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["/ko-app/coder-snapshot-portal"]
