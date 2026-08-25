package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/valyala/fasthttp"
	"github.com/valyala/fasthttp/fasthttputil"
)

func TestValidateFields(t *testing.T) {
	payload, status := validateFields(map[string]any{"name": "Ada", "age": json.Number("42")})
	if status != 200 {
		t.Fatalf("status=%d payload=%v", status, payload)
	}
	ok := payload.(validateOK)
	if !ok.Valid || ok.Name != "Ada" || ok.Age != 42 {
		t.Fatalf("unexpected payload %#v", ok)
	}

	_, status = validateFields(map[string]any{"name": "", "age": json.Number("42")})
	if status != 400 {
		t.Fatalf("empty name status=%d", status)
	}
	_, status = validateFields(map[string]any{"name": "Ada", "age": json.Number("999")})
	if status != 400 {
		t.Fatalf("bad age status=%d", status)
	}
	_, status = validateFields(map[string]any{"name": "Ada", "age": 42.5})
	if status != 400 {
		t.Fatalf("float age status=%d", status)
	}
}

func TestNetHTTPRoutes(t *testing.T) {
	server := httptest.NewServer(netHTTPHandler())
	defer server.Close()
	assertRouteParity(t, server.URL)
}

func TestFastHTTPRoutes(t *testing.T) {
	ln := fasthttputil.NewInmemoryListener()
	go func() {
		_ = fasthttp.Serve(ln, fastHTTPHandler)
	}()
	t.Cleanup(func() { _ = ln.Close() })

	client := &http.Client{
		Transport: &http.Transport{
			Dial: func(network, addr string) (net.Conn, error) {
				return ln.Dial()
			},
		},
		Timeout: 5 * time.Second,
	}
	assertRouteParityWithClient(t, "http://bench", client)
}

func assertRouteParity(t *testing.T, base string) {
	t.Helper()
	assertRouteParityWithClient(t, base, http.DefaultClient)
}

func assertRouteParityWithClient(t *testing.T, base string, client *http.Client) {
	t.Helper()

	res := mustDo(t, client, http.MethodGet, base+"/plaintext", "", nil)
	if res.status != 200 || res.body != hello {
		t.Fatalf("plaintext: status=%d body=%q", res.status, res.body)
	}
	if !strings.HasPrefix(res.contentType, "text/plain") {
		t.Fatalf("plaintext content-type %q", res.contentType)
	}

	res = mustDo(t, client, http.MethodGet, base+"/json", "", nil)
	assertJSON(t, res, 200, map[string]any{"message": hello})

	res = mustDo(t, client, http.MethodGet, base+"/user/42", "", nil)
	assertJSON(t, res, 200, map[string]any{"id": "42", "name": "User 42"})

	res = mustDo(t, client, http.MethodPost, base+"/validate", "application/json", []byte(`{"name":"Ada","age":42}`))
	assertJSON(t, res, 200, map[string]any{"name": "Ada", "age": float64(42), "valid": true})

	res = mustDo(t, client, http.MethodPost, base+"/validate", "application/json", []byte(`{"name":"","age":42}`))
	if res.status != 400 {
		t.Fatalf("validate invalid: status=%d body=%s", res.status, res.body)
	}

	res = mustDo(t, client, http.MethodPost, base+"/form", "application/x-www-form-urlencoded", []byte("name=Ada&age=42&source=benchmark"))
	if res.status != 204 {
		t.Fatalf("form: status=%d", res.status)
	}

	payload := bytes.Repeat([]byte("x"), 1024)
	res = mustDo(t, client, http.MethodPost, base+"/echo/json", "application/json", payload)
	assertJSON(t, res, 200, map[string]any{"bytes": float64(1024)})

	res = mustDo(t, client, http.MethodPost, base+"/ingest/64k", "application/octet-stream", bytes.Repeat([]byte{1}, 64))
	assertJSON(t, res, 200, map[string]any{"bytes": float64(64)})

	res = mustDo(t, client, http.MethodPost, base+"/ingest/2m", "application/octet-stream", bytes.Repeat([]byte{2}, 128))
	assertJSON(t, res, 200, map[string]any{"bytes": float64(128)})

	res = mustDo(t, client, http.MethodPost, base+"/ingest/stream/2m", "application/octet-stream", bytes.Repeat([]byte{3}, 256))
	assertJSON(t, res, 200, map[string]any{"bytes": float64(256)})

	multipart := []byte("--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\nContent-Type: application/octet-stream\r\n\r\nxxxx\r\n--x--\r\n")
	res = mustDo(t, client, http.MethodPost, base+"/upload", "multipart/form-data; boundary=x", multipart)
	assertJSON(t, res, 200, map[string]any{"bytes": float64(len(multipart))})
}

type httpResult struct {
	status      int
	body        string
	contentType string
}

func mustDo(t *testing.T, client *http.Client, method, url, contentType string, body []byte) httpResult {
	t.Helper()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, url, reader)
	if err != nil {
		t.Fatal(err)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return httpResult{status: resp.StatusCode, body: string(raw), contentType: resp.Header.Get("Content-Type")}
}

func assertJSON(t *testing.T, res httpResult, status int, want map[string]any) {
	t.Helper()
	if res.status != status {
		t.Fatalf("status=%d want=%d body=%s", res.status, status, res.body)
	}
	if !strings.HasPrefix(res.contentType, jsonContentType) {
		t.Fatalf("content-type %q", res.contentType)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(res.body), &got); err != nil {
		t.Fatalf("json: %v body=%s", err, res.body)
	}
	for key, value := range want {
		if got[key] != value {
			t.Fatalf("json[%q]=%v want=%v body=%s", key, got[key], value, res.body)
		}
	}
}
