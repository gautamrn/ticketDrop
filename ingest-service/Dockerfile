FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/ingest ./cmd/ingest \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/mock-ra ./cmd/mock-ra

FROM alpine:3.22
RUN apk add --no-cache ca-certificates \
    && addgroup -S ticketdrop \
    && adduser -S -G ticketdrop ticketdrop
COPY --from=build /out/ingest /usr/local/bin/ingest
USER ticketdrop
EXPOSE 2112
ENTRYPOINT ["ingest"]
