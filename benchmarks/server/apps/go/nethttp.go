package main

import (
	"io"
	"net/http"
	"time"
)

func netHTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /plaintext", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", textContentType)
		_, _ = io.WriteString(w, hello)
	})
	mux.HandleFunc("GET /json", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, helloJSON())
	})
	mux.HandleFunc("GET /user/{id}", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		writeJSON(w, http.StatusOK, userJSON(id))
	})
	mux.HandleFunc("POST /validate", func(w http.ResponseWriter, r *http.Request) {
		body, err := readAll(r.Body)
		if err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		data, err := decodeObject(body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, marshalJSON(validateErr{Error: "invalid json"}))
			return
		}
		payload, status := validateFields(data)
		writeJSON(w, status, marshalJSON(payload))
	})
	mux.HandleFunc("POST /form", func(w http.ResponseWriter, r *http.Request) {
		_, _ = readAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("POST /echo/json", bufferedBytes(muxWriteJSON))
	mux.HandleFunc("POST /ingest/64k", bufferedBytes(muxWriteJSON))
	mux.HandleFunc("POST /ingest/2m", bufferedBytes(muxWriteJSON))
	mux.HandleFunc("POST /ingest/stream/2m", func(w http.ResponseWriter, r *http.Request) {
		n, err := countStream(r.Body)
		if err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		writeJSON(w, http.StatusOK, bytesJSON(n))
	})
	mux.HandleFunc("POST /upload", bufferedBytes(muxWriteJSON))
	return mux
}

type jsonWriter func(http.ResponseWriter, int, []byte)

func muxWriteJSON(w http.ResponseWriter, status int, body []byte) {
	writeJSON(w, status, body)
}

func bufferedBytes(write jsonWriter) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := readAll(r.Body)
		if err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		write(w, http.StatusOK, bytesJSON(len(body)))
	}
}

func writeJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", jsonContentType)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func serveNetHTTP(addr string) error {
	server := &http.Server{
		Addr:              addr,
		Handler:           netHTTPHandler(),
		ReadHeaderTimeout: 30 * time.Second,
		ReadTimeout:       120 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 16,
	}
	return server.ListenAndServe()
}
