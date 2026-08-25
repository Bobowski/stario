package main

import (
	"bytes"

	"github.com/valyala/fasthttp"
)

func serveFastHTTP(addr string) error {
	server := &fasthttp.Server{
		Handler:            fastHTTPHandler,
		Name:               "",
		ReadTimeout:        0,
		WriteTimeout:       0,
		IdleTimeout:        0,
		MaxRequestBodySize:           4 * 1024 * 1024,
		StreamRequestBody:            true,
		DisablePreParseMultipartForm: true,
		DisableKeepalive:             false,
		ReduceMemoryUsage:  false,
		// Keep Date. Python targets also emit Date; stripping it is a
		// TechEmpower trick, not route-parity.
		NoDefaultServerHeader: true,
	}
	return server.ListenAndServe(addr)
}

func fastHTTPHandler(ctx *fasthttp.RequestCtx) {
	path := string(ctx.Path())
	method := string(ctx.Method())

	switch {
	case method == fasthttp.MethodGet && path == "/plaintext":
		ctx.SetContentType(textContentType)
		ctx.SetStatusCode(fasthttp.StatusOK)
		_, _ = ctx.WriteString(hello)
	case method == fasthttp.MethodGet && path == "/json":
		writeFastJSON(ctx, fasthttp.StatusOK, helloJSON())
	case method == fasthttp.MethodGet && bytes.HasPrefix(ctx.Path(), []byte("/user/")):
		writeFastJSON(ctx, fasthttp.StatusOK, userJSON(userIDFromPath(path)))
	case method == fasthttp.MethodPost && path == "/validate":
		body, err := fastReadBuffer(ctx)
		if err != nil {
			ctx.SetStatusCode(fasthttp.StatusBadRequest)
			return
		}
		data, err := decodeObject(body)
		if err != nil {
			writeFastJSON(ctx, fasthttp.StatusBadRequest, marshalJSON(validateErr{Error: "invalid json"}))
			return
		}
		payload, status := validateFields(data)
		writeFastJSON(ctx, status, marshalJSON(payload))
	case method == fasthttp.MethodPost && path == "/form":
		_, _ = fastReadBuffer(ctx)
		ctx.SetStatusCode(fasthttp.StatusNoContent)
	case method == fasthttp.MethodPost && (path == "/echo/json" || path == "/ingest/64k" || path == "/ingest/2m" || path == "/upload"):
		body, err := fastReadBuffer(ctx)
		if err != nil {
			ctx.SetStatusCode(fasthttp.StatusBadRequest)
			return
		}
		writeFastJSON(ctx, fasthttp.StatusOK, bytesJSON(len(body)))
	case method == fasthttp.MethodPost && path == "/ingest/stream/2m":
		n, err := fastCountStream(ctx)
		if err != nil {
			ctx.SetStatusCode(fasthttp.StatusBadRequest)
			return
		}
		writeFastJSON(ctx, fasthttp.StatusOK, bytesJSON(n))
	default:
		ctx.SetStatusCode(fasthttp.StatusNotFound)
		_, _ = ctx.WriteString("Not Found")
	}
}

func writeFastJSON(ctx *fasthttp.RequestCtx, status int, body []byte) {
	ctx.SetContentType(jsonContentType)
	ctx.SetStatusCode(status)
	_, _ = ctx.Write(body)
}

func fastReadBuffer(ctx *fasthttp.RequestCtx) ([]byte, error) {
	if ctx.IsBodyStream() {
		return readAll(ctx.RequestBodyStream())
	}
	return append([]byte(nil), ctx.PostBody()...), nil
}

func fastCountStream(ctx *fasthttp.RequestCtx) (int, error) {
	if ctx.IsBodyStream() {
		return countStream(ctx.RequestBodyStream())
	}
	return countStream(bytes.NewReader(ctx.PostBody()))
}
