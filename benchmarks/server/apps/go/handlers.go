package main

import (
	"bytes"
	"encoding/json"
	"io"
	"math"
	"strconv"
)

const (
	hello           = "Hello, World!"
	jsonContentType = "application/json"
	textContentType = "text/plain; charset=utf-8"
)

type jsonMessage struct {
	Message string `json:"message"`
}

type userMessage struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type byteCount struct {
	Bytes int `json:"bytes"`
}

type validateOK struct {
	Name  string `json:"name"`
	Age   int    `json:"age"`
	Valid bool   `json:"valid"`
}

type validateErr struct {
	Error string `json:"error"`
}

func marshalJSON(value any) []byte {
	// encoding/json each request — no cached response bytes (handler policy).
	payload, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return payload
}

func helloJSON() []byte {
	return marshalJSON(jsonMessage{Message: hello})
}

func userJSON(userID string) []byte {
	return marshalJSON(userMessage{ID: userID, Name: "User " + userID})
}

func bytesJSON(n int) []byte {
	return marshalJSON(byteCount{Bytes: n})
}

func validateFields(data map[string]any) (any, int) {
	name, ok := data["name"].(string)
	if !ok || name == "" {
		return validateErr{Error: "name must be a non-empty string"}, 400
	}
	age, ok := asInt(data["age"])
	if !ok || age < 0 || age > 150 {
		return validateErr{Error: "age must be an integer between 0 and 150"}, 400
	}
	return validateOK{Name: name, Age: age, Valid: true}, 200
}

func asInt(value any) (int, bool) {
	switch n := value.(type) {
	case json.Number:
		i, err := n.Int64()
		if err != nil {
			return 0, false
		}
		return int(i), true
	case float64:
		if n != math.Trunc(n) {
			return 0, false
		}
		return int(n), true
	case int:
		return n, true
	case int64:
		return int(n), true
	default:
		return 0, false
	}
}

func decodeObject(body []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var data map[string]any
	if err := dec.Decode(&data); err != nil {
		return nil, err
	}
	return data, nil
}

func readAll(r io.Reader) ([]byte, error) {
	return io.ReadAll(r)
}

func countStream(r io.Reader) (int, error) {
	n, err := io.Copy(io.Discard, r)
	return int(n), err
}

func userIDFromPath(path string) string {
	const prefix = "/user/"
	if len(path) <= len(prefix) || path[:len(prefix)] != prefix {
		return ""
	}
	return path[len(prefix):]
}

func atoiDefault(s string, fallback int) int {
	if s == "" {
		return fallback
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return fallback
	}
	return n
}
